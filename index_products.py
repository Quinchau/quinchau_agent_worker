import os
import pymysql
from sentence_transformers import SentenceTransformer
from qdrant_client import QdrantClient
from qdrant_client.models import PointStruct
import hashlib
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def sanitize_id(stockid):
    try:
        return int(stockid)
    except (ValueError, TypeError):
        h = hashlib.md5(str(stockid).encode())
        return int(h.hexdigest()[:16], 16) % (2**63 - 1)

def search_test(query_text="bujia del nk", limit=5):
    logger.info(f"🔍 Buscando: '{query_text}'")
    
    print("Cargando modelo CLIP...")
    model = SentenceTransformer('sentence-transformers/clip-ViT-B-32-multilingual-v1')
    
    print("Conectando a Qdrant...")
    qdrant_client = QdrantClient(host='qdrant', port=6333)
    
    print("Generando embedding de la consulta...")
    query_embedding = model.encode(query_text)
    
    # Sintaxis correcta para qdrant-client v1.7+
    results = qdrant_client.query_points(
        collection_name='quinchau_productos',
        query=query_embedding.tolist(),
        limit=limit
    )
    
    print("\n📋 RESULTADOS:")
    for i, result in enumerate(results.points):
        print(f"{i+1}. Score: {result.score:.4f}")
        print(f"   ID: {result.id}")
        print(f"   Descripción: {result.payload.get('description', 'N/A')[:80]}")
        print()

def index_products():
    logger.info("Iniciando indexación...")
    
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
            SELECT stockid, description, longdescription, categoryid, units, actualcost
            FROM stockmaster
            WHERE discontinued = 0 AND description IS NOT NULL AND description != ''
        """)
        products = cursor.fetchall()
    print(f"Encontrados {len(products)} productos")
    
    print("Cargando modelo CLIP...")
    model = SentenceTransformer('sentence-transformers/clip-ViT-B-32-multilingual-v1')
    
    print("Conectando a Qdrant...")
    qdrant = QdrantClient(host='qdrant', port=6333)
    
    print("Generando embeddings e indexando...")
    points = []
    for i, product in enumerate(products):
        text = f"{product['description']} {product.get('longdescription', '')}"
        embedding = model.encode(text)
        point_id = sanitize_id(product['stockid'])
        point = PointStruct(
            id=point_id,
            vector=embedding.tolist(),
            payload={
                'stockid': product['stockid'],
                'description': product['description'],
                'longdescription': product.get('longdescription', '')[:200],
                'categoryid': product.get('categoryid', '')
            }
        )
        points.append(point)
        
        if len(points) >= 50:
            qdrant.upsert(collection_name='quinchau_productos', points=points)
            print(f"  Batch: {i+1}/{len(products)}")
            points = []
    
    if points:
        qdrant.upsert(collection_name='quinchau_productos', points=points)
    
    print(f"✅ Indexación completada: {len(products)} productos")
    conn.close()

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--test', action='store_true')
    parser.add_argument('--query', type=str, default='bujia del nk')
    args = parser.parse_args()
    
    if args.test:
        search_test(args.query)
    else:
        index_products()