import os
import sys
import time
import logging
import pymysql
import hashlib
import unicodedata
from openai import OpenAI
from qdrant_client import QdrantClient
from qdrant_client.models import PointStruct, VectorParams, Distance

# ─────────────────────────────────────────
# LOGGING
# ─────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(message)s',
    handlers=[
        logging.FileHandler('/var/log/reindex_multi_term.log', encoding='utf-8'),
        logging.StreamHandler()
    ]
)
log = logging.getLogger(__name__)

# ─────────────────────────────────────────
# CONFIGURACIÓN
# ─────────────────────────────────────────
BATCH_SIZE        = 50
MAX_TEXT_CHARS    = 2000
MAX_RETRIES       = 3
RETRY_DELAY       = 5
PROGRESS_INTERVAL = 500
COLLECTION        = 'quinchau_productos'
VECTOR_SIZE       = 1536  # text-embedding-3-small

# ─────────────────────────────────────────
# NORMALIZACIÓN DE TEXTO
# ─────────────────────────────────────────
def normalizar_texto(texto: str) -> str:
    """
    Normaliza texto para embeddings:
    - Convierte a minúsculas
    - Elimina acentos (í -> i, é -> e, etc.)
    """
    if not texto:
        return ""
    texto = texto.lower()
    texto = unicodedata.normalize('NFKD', texto).encode('ASCII', 'ignore').decode('utf-8')
    return texto

# ─────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────
def sanitize_id(stockid):
    try:
        return int(stockid)
    except Exception:
        h = hashlib.md5(str(stockid).encode())
        return int(h.hexdigest()[:16], 16) % (2**63 - 1)


def clean_description(desc):
    """Elimina el prefijo de código (ej: '000-901 Pastilla...')."""
    if desc and desc[0].isdigit() and '-' in desc[:10]:
        parts = desc.split(' ', 1)
        if len(parts) > 1:
            return parts[1].strip()
    return desc.strip() if desc else ''


def build_text(product):
    """
    Texto limpio: solo descripción (sin modelos compatibles).
    AHORA NORMALIZADO: minúsculas y sin acentos.
    """
    raw  = product.get('description', '') or product.get('longdescription', '') or ''
    desc = clean_description(raw)

    # SOLO la descripción, sin modelos compatibles
    texto = desc

    # NORMALIZAR ANTES DE VECTORIZAR
    return normalizar_texto(texto[:MAX_TEXT_CHARS])


def get_embeddings_with_retry(client, textos):
    """Llama a la API en batch con reintentos ante fallos."""
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            response = client.embeddings.create(
                model="openai/text-embedding-3-small",
                input=textos
            )
            return [item.embedding for item in response.data]
        except Exception as e:
            log.warning(f"   ⚠️  Intento {attempt}/{MAX_RETRIES} fallido: {e}")
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_DELAY * attempt)
            else:
                raise


def ensure_collection(qdrant):
    """Crea la colección si no existe."""
    try:
        qdrant.get_collection(COLLECTION)
    except Exception:
        log.info(f"   Colección no existe, creando...")
        qdrant.create_collection(
            collection_name=COLLECTION,
            vectors_config=VectorParams(size=VECTOR_SIZE, distance=Distance.COSINE)
        )
        log.info(f"   ✅ Colección '{COLLECTION}' creada")


def load_indexed_ids(qdrant):
    """Retorna set de IDs ya indexados. Soporta colección inexistente."""
    indexed = set()
    offset  = None
    try:
        while True:
            result, offset = qdrant.scroll(
                collection_name=COLLECTION,
                limit=1000,
                offset=offset,
                with_payload=False,
                with_vectors=False
            )
            for point in result:
                indexed.add(point.id)
            if offset is None:
                break
    except Exception as e:
        if '404' in str(e) or "doesn't exist" in str(e):
            pass  # colección vacía, se crea luego
        else:
            raise
    return indexed


# ─────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────
log.info("=" * 60)
log.info("INDEXACIÓN CON NORMALIZACIÓN - minúsculas + sin acentos")
log.info("=" * 60)

FORCE = '--force' in sys.argv

conn = pymysql.connect(
    host='db', port=3306,
    user='tum12607_webmas2', password='6060',
    database='tum12607_maracay',
    cursorclass=pymysql.cursors.DictCursor
)
cursor = conn.cursor()

# Cargar productos
cursor.execute("""
    SELECT
        s.stockid,
        s.description,
        s.longdescription,
        GROUP_CONCAT(
            DISTINCT CONCAT(m.modeldescrip, ' (', ma.marcadescrip, ')')
            SEPARATOR ', '
        ) as modelos_compatibles
    FROM stockmaster s
    LEFT JOIN stockmaster_modelo sm ON s.stockid = sm.stockid
    LEFT JOIN modelos m ON sm.idmodelo = m.idmodelo
    LEFT JOIN marcas ma ON m.idmarca = ma.idmarca
    WHERE s.discontinued = 0
    GROUP BY s.stockid
""")
products = cursor.fetchall()
log.info(f"✅ Encontrados {len(products)} productos")

# Clientes externos
client = OpenAI(
    base_url="https://openrouter.ai/api/v1",
    api_key=os.getenv("OPENROUTER_API_KEY")
)
qdrant = QdrantClient(host='qdrant', port=6333)

# Garantizar colección
ensure_collection(qdrant)

# IDs ya indexados
if FORCE:
    log.info("⚡ Modo --force: reindexando todos los productos")
    already_indexed = set()
else:
    log.info("🔍 Verificando productos ya indexados...")
    already_indexed = load_indexed_ids(qdrant)
    log.info(f"   Ya indexados: {len(already_indexed)}")

pending = [p for p in products if sanitize_id(p['stockid']) not in already_indexed]
log.info(f"📦 Pendientes de indexar: {len(pending)}")

if not pending:
    log.info("✅ Todo ya estaba indexado. Nada que hacer.")
    conn.close()
    exit(0)

# ─── Indexación por batches ───
log.info(f"\n🚀 Iniciando indexación en batches de {BATCH_SIZE}...\n")

total_ok    = 0
total_error = 0

for batch_start in range(0, len(pending), BATCH_SIZE):
    batch  = pending[batch_start : batch_start + BATCH_SIZE]
    textos = [build_text(p) for p in batch]

    # Log ejemplo primer batch
    if batch_start == 0:
        p0 = batch[0]
        log.info("📝 Ejemplo primer producto:")
        log.info(f"   ID    : {p0['stockid']}")
        log.info(f"   Texto : {textos[0][:200]}")
        log.info("")

    # Log producto objetivo si está en este batch
    for idx, product in enumerate(batch):
        if product['stockid'] == '918-427':
            log.info(f"📝 Producto objetivo (918-427): {textos[idx]}")

    # Embeddings
    try:
        embeddings = get_embeddings_with_retry(client, textos)
    except Exception as e:
        log.error(f"❌ Batch {batch_start} falló definitivamente: {e}")
        total_error += len(batch)
        continue

    # Upsert Qdrant - AHORA description USA EL TEXTO NORMALIZADO
    points = [
        PointStruct(
            id=sanitize_id(p['stockid']),
            vector=emb,
            payload={
                'stockid_original': p['stockid'],
                'description':      textos[idx],  # ← TEXTO NORMALIZADO (minúsculas, sin acentos)
                'description_original': p.get('description', ''),  # ← Opcional: guardar original si se necesita
                'modelos':          p.get('modelos_compatibles', '')
            }
        )
        for idx, (p, emb) in enumerate(zip(batch, embeddings))
    ]

    try:
        qdrant.upsert(collection_name=COLLECTION, points=points)
        total_ok += len(batch)
    except Exception as e:
        log.error(f"❌ Qdrant upsert falló en batch {batch_start}: {e}")
        total_error += len(batch)
        continue

    done = batch_start + len(batch)
    if done % PROGRESS_INTERVAL < BATCH_SIZE or done >= len(pending):
        log.info(f"   ✔ Indexados {done}/{len(pending)} | OK: {total_ok} | Errores: {total_error}")

log.info("")
log.info("=" * 60)
log.info("✅ INDEXACIÓN COMPLETADA")
log.info(f"   Total procesados : {total_ok + total_error}")
log.info(f"   Exitosos         : {total_ok}")
log.info(f"   Con error        : {total_error}")
log.info("=" * 60)

conn.close()