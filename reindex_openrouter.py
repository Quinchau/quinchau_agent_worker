import os
import pymysql
import hashlib
import time
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

print("Obteniendo productos...")
with conn.cursor() as cursor:
    cursor.execute("""
        SELECT stockid, description, longdescription
        FROM stockmaster
        WHERE discontinued = 0 AND description IS NOT NULL AND description != ''
    """)
    products = cursor.fetchall()
print(f"Total: {len(products)} productos")

client = OpenAI(
    base_url="https://openrouter.ai/api/v1",
    api_key=os.getenv("OPENROUTER_API_KEY")
)

qdrant = QdrantClient(host='qdrant', port=6333)

print("Indexando productos...")
for i, product in enumerate(products):
    text = f"{product['description']} {product.get('longdescription', '')}"
    
    response = client.embeddings.create(
        model="openai/text-embedding-3-small",
        input=text[:1000]
    )
    
    point = PointStruct(
        id=sanitize_id(product['stockid']),
        vector=response.data[0].embedding,
        payload={
            'stockid_original': product['stockid'],
            'description': product['description']
        }
    )
    qdrant.upsert(collection_name='quinchau_productos', points=[point])
    
    if (i + 1) % 50 == 0:
        print(f"  Indexados {i + 1}/{len(products)}")

print(f"✅ Completado: {len(products)} productos")
conn.close()