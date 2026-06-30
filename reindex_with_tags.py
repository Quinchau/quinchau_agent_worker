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

print("=" * 60)
print("Indexación con múltiples tags")
print("=" * 60)

conn = pymysql.connect(
    host='db', port=3306,
    user='tum12607_webmas2', password='6060',
    database='tum12607_maracay',
    cursorclass=pymysql.cursors.DictCursor
)

# Cargar términos
cursor = conn.cursor()
cursor.execute('''
    SELECT t.termino, GROUP_CONCAT(a.alias SEPARATOR '|') as alias
    FROM terminos_semanticos t
    LEFT JOIN terminos_alias a ON t.id = a.id_termino
    WHERE t.activo = 1
    GROUP BY t.id
''')

TERMINOS_CACHE = {}
for row in cursor.fetchall():
    termino = row['termino'].lower()
    if row['alias']:
        TERMINOS_CACHE[termino] = row['alias'].split('|')
    else:
        TERMINOS_CACHE[termino] = []
print(f"✅ Cargados {len(TERMINOS_CACHE)} términos")

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
    AND (s.description LIKE '%Automatico Arranque%' 
         OR s.longdescription LIKE '%Automatico Arranque%'
         OR s.description LIKE '%Faro%'
         OR s.longdescription LIKE '%Faro%')
    GROUP BY s.stockid
""")

products = cursor.fetchall()
print(f"✅ Encontrados {len(products)} productos")

def build_text_with_all_tags(product):
    desc = product.get('description', '') or product.get('longdescription', '')
    
    # Limpiar código
    if desc and desc[0].isdigit() and '-' in desc[:10]:
        parts = desc.split(' ', 1)
        if len(parts) > 1:
            desc = parts[1]
    
    # Buscar TODOS los términos
    texto_lower = desc.lower()
    tags_encontrados = []
    
    for termino, alias_lista in TERMINOS_CACHE.items():
        if termino in texto_lower:
            tags_encontrados.extend(alias_lista)
            print(f"     Encontrado término: {termino} -> {alias_lista}")
    
    # Construir texto
    if tags_encontrados:
        tags_unicos = list(set(tags_encontrados))
        texto = f"{desc}, {', '.join(tags_unicos)}"
    else:
        texto = desc
    
    # Agregar modelos
    if product.get('modelos_compatibles'):
        texto += f". Compatible con {product['modelos_compatibles']}"
    
    return texto

client = OpenAI(
    base_url="https://openrouter.ai/api/v1",
    api_key=os.getenv("OPENROUTER_API_KEY")
)

qdrant = QdrantClient(host='qdrant', port=6333)

print(f"\n📦 Indexando {len(products)} productos...")

for i, product in enumerate(products):
    texto = build_text_with_all_tags(product)
    
    if i == 0:
        print(f"\n📝 Ejemplo:")
        print(f"   {texto}")
    
    response = client.embeddings.create(
        model="openai/text-embedding-3-small",
        input=texto[:1000]
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
    
    if (i + 1) % 10 == 0:
        print(f"   Indexados {i + 1}/{len(products)}")

print(f"\n✅ Completado: {len(products)} productos")
conn.close()