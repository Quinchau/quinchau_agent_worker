import os
import pymysql
import hashlib
from openai import OpenAI
from qdrant_client import QdrantClient
from qdrant_client.models import PointStruct

def sanitize_id(stockid):
    try:
        return int(stockid)
    except:
        h = hashlib.md5(str(stockid).encode())
        return int(h.hexdigest()[:16], 16) % (2**63 - 1)

print("Conectando a MySQL...")
conn = pymysql.connect(
    host='db', port=3306,
    user='tum12607_webmas2', password='6060',
    database='tum12607_maracay',
    cursorclass=pymysql.cursors.DictCursor
)

print("Obteniendo productos con modelos y marcas...")
with conn.cursor() as cursor:
    cursor.execute("""
        SELECT 
            s.stockid,
            s.description,
            s.longdescription,
            GROUP_CONCAT(DISTINCT CONCAT(m.modeldescrip, ' (', ma.marcadescrip, ')') SEPARATOR ', ') as modelos_compatibles
        FROM stockmaster s
        LEFT JOIN stockmaster_modelo sm ON s.stockid = sm.stockid
        LEFT JOIN modelos m ON sm.idmodelo = m.idmodelo
        LEFT JOIN marcas ma ON m.idmarca = ma.idmarca
        WHERE s.discontinued = 0
        GROUP BY s.stockid
    """)
    products = cursor.fetchall()
print(f"Total: {len(products)} productos")

def build_text_to_embed(product):
    # Usar longdescription como base
    texto = product.get('longdescription', '') or product.get('description', '')
    
    # Agregar modelos compatibles
    if product.get('modelos_compatibles'):
        texto += f" | Compatible con: {product['modelos_compatibles']}"
    
    return texto

client = OpenAI(
    base_url="https://openrouter.ai/api/v1",
    api_key=os.getenv("OPENROUTER_API_KEY")
)

qdrant = QdrantClient(host='qdrant', port=6333)

print("Indexando productos con contexto enriquecido...")
for i, product in enumerate(products):
    text = build_text_to_embed(product)
    
    response = client.embeddings.create(
        model="openai/text-embedding-3-small",
        input=text[:1000]
    )
    
    point = PointStruct(
        id=sanitize_id(product['stockid']),
        vector=response.data[0].embedding,
        payload={
            'stockid_original': product['stockid'],
            'description': product.get('description', ''),
            'modelos': product.get('modelos_compatibles', '')
        }
    )
    qdrant.upsert(collection_name='quinchau_productos', points=[point])
    
    if (i + 1) % 50 == 0:
        print(f"  Indexados {i + 1}/{len(products)}")

print(f"✅ Completado: {len(products)} productos")
conn.close()