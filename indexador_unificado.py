#!/usr/bin/env python3
"""
Indexador unificado para Qdrant
- Texto: OpenRouter (text-embedding-3-small)
- Imágenes: OpenRouter (google/gemini-embedding-2-preview, input como string plano)
TODO usando OpenRouter - SIN dependencias pesadas

Novedades de esta versión:
- Soporte de indexado en lote: --batch N
- Redimensiona/recomprime imágenes antes de mandarlas (controla costo y evita
  exceder el límite de contexto de 8192 tokens del modelo)
- Trackea tokens y costo real (texto + imagen) y lo resume al final del batch
"""
import os
import sys
import time
import logging
import pymysql
import hashlib
import numpy as np
import io
import httpx
from typing import List, Dict, Optional
from openai import OpenAI
from qdrant_client import QdrantClient
from qdrant_client.models import PointStruct, VectorParams, Distance
from datetime import datetime
import unicodedata
import base64
import argparse

try:
    from PIL import Image
    PIL_AVAILABLE = True
except ImportError:
    PIL_AVAILABLE = False

# ─────────────────────────────────────────
# CONFIGURACIÓN
# ─────────────────────────────────────────
BATCH_SIZE_TEXTO = 100
BATCH_SIZE_IMAGENES = 20
MAX_RETRIES = 3
RETRY_DELAY = 5
PROGRESS_INTERVAL = 100
MAX_TEXT_CHARS = 2000

COLLECTION_TEXTO = 'quinchau_productos'
COLLECTION_IMAGENES = 'quinchau_productos_imagenes'

MODELO_TEXTO = "openai/text-embedding-3-small"
MODELO_IMAGEN = "google/gemini-embedding-2-preview"

VECTOR_SIZE_TEXTO = 1536
VECTOR_SIZE_IMAGEN_FALLBACK = 3072

# Redimensionado de imágenes antes de embedear (controla costo y contexto)
RESIZE_MAX_DIM = 512
JPEG_QUALITY = 85

# Precios (USD por token) — gemini-embedding-2-preview se factura acá como
# texto porque el formato que funciona (input=[data_uri]) manda el base64
# como string, no por el path de visión con tarifa especial.
PRICE_PER_TOKEN_TEXTO = 0.02 / 1_000_000    # openai/text-embedding-3-small
PRICE_PER_TOKEN_IMAGEN = 0.20 / 1_000_000   # google/gemini-embedding-2-preview

BASE_IMG_URL = os.getenv('IMG_BASE_URL', 'https://quinchau.com/weberp/img/p/')
OPENROUTER_API_KEY = os.getenv('OPENROUTER_API_KEY')

# ─────────────────────────────────────────
# LOGGING
# ─────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(message)s',
    handlers=[
        logging.FileHandler('/var/log/indexador_unificado.log', encoding='utf-8'),
        logging.StreamHandler()
    ]
)
log = logging.getLogger(__name__)

if not PIL_AVAILABLE:
    log.warning(
        "⚠️ Pillow no está instalado — las imágenes se mandan SIN redimensionar. "
        "Esto puede disparar el costo y hacer que imágenes grandes excedan el "
        "límite de contexto (8192 tokens). Instalá con: "
        "pip install Pillow --break-system-packages"
    )

# ─────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────
def sanitize_id(stockid):
    try:
        return int(stockid)
    except Exception:
        h = hashlib.md5(str(stockid).encode())
        return int(h.hexdigest()[:16], 16) % (2**63 - 1)

def normalizar_texto(texto: str) -> str:
    if not texto:
        return ""
    texto = texto.lower()
    texto = unicodedata.normalize('NFKD', texto).encode('ASCII', 'ignore').decode('utf-8')
    return texto

def clean_description(desc):
    if desc and desc[0].isdigit() and '-' in desc[:10]:
        parts = desc.split(' ', 1)
        if len(parts) > 1:
            return parts[1].strip()
    return desc.strip() if desc else ''

def preparar_imagen(raw_bytes: bytes) -> bytes:
    """
    Redimensiona a RESIZE_MAX_DIM px (lado mayor) y recomprime a JPEG.
    Esto controla el costo (menos bytes -> menos tokens de texto en el
    base64) y evita que fotos grandes exceden el límite de contexto del
    modelo. Si Pillow no está disponible, devuelve los bytes originales.
    """
    if not PIL_AVAILABLE:
        return raw_bytes
    try:
        img = Image.open(io.BytesIO(raw_bytes))
        img = img.convert('RGB')
        img.thumbnail((RESIZE_MAX_DIM, RESIZE_MAX_DIM))
        buf = io.BytesIO()
        img.save(buf, format='JPEG', quality=JPEG_QUALITY)
        return buf.getvalue()
    except Exception as e:
        log.warning(f"   ⚠️ No se pudo redimensionar la imagen ({e}), uso el original")
        return raw_bytes

# ─────────────────────────────────────────
# CLIENTE OPENROUTER UNIFICADO
# ─────────────────────────────────────────
class OpenRouterEmbedder:
    """
    Cliente unificado para OpenRouter
    - Texto: embeddings estándar (input = lista de strings)
    - Imágenes: embeddings vía string plano (input = [data_uri])
    Trackea tokens/costo acumulado en self.tokens_texto / self.tokens_imagen.
    """

    def __init__(self):
        self.client = OpenAI(
            base_url="https://openrouter.ai/api/v1",
            api_key=OPENROUTER_API_KEY,
        )
        self.tokens_texto = 0
        self.tokens_imagen = 0
        log.info("🔧 OpenRouter Embedder inicializado")
        log.info(f"   Modelo texto: {MODELO_TEXTO}")
        log.info(f"   Modelo imagen: {MODELO_IMAGEN}")

    # ────────────────────────────────────
    # EMBEDDING DE TEXTO
    # ────────────────────────────────────
    def get_text_embeddings_batch(self, textos: List[str]) -> List[List[float]]:
        """Genera embeddings de texto en batch"""
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                response = self.client.embeddings.create(
                    model=MODELO_TEXTO,
                    input=textos,
                    encoding_format="float"
                )
                try:
                    self.tokens_texto += response.usage.prompt_tokens
                except Exception:
                    pass
                return [item.embedding for item in response.data]
            except Exception as e:
                log.warning(f"   ⚠️ Texto intento {attempt}/{MAX_RETRIES} fallido: {e}")
                if attempt < MAX_RETRIES:
                    time.sleep(RETRY_DELAY * attempt)
                else:
                    raise
        return []

    # ────────────────────────────────────
    # EMBEDDING DE IMAGEN
    # ────────────────────────────────────
    def get_image_embedding(self, image_url: str) -> Optional[List[float]]:
        """
        google/gemini-embedding-2-preview via OpenRouter.
        Formato: input=[data_uri] (string plano) + encoding_format="float"
        (ver notas de versiones anteriores del script para el porqué).
        """
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                # 1. Descargar imagen
                response = httpx.get(image_url, timeout=30.0)
                response.raise_for_status()

                # 2. Redimensionar/recomprimir para controlar costo y contexto
                image_bytes = preparar_imagen(response.content)

                # 3. Convertir a base64 + data URI
                image_base64 = base64.b64encode(image_bytes).decode('utf-8')
                data_uri = f"data:image/jpeg;base64,{image_base64}"

                # 4. Llamar a OpenRouter
                embedding_response = self.client.embeddings.create(
                    model=MODELO_IMAGEN,
                    input=[data_uri],
                    encoding_format="float"
                )

                try:
                    self.tokens_imagen += embedding_response.usage.prompt_tokens
                except Exception:
                    pass

                if embedding_response.data:
                    log.info(f"   ✅ Embedding generado (dim: {len(embedding_response.data[0].embedding)})")
                    return embedding_response.data[0].embedding
                else:
                    log.warning(f"⚠️ No se recibió embedding para {image_url}")
                    return None

            except Exception as e:
                log.warning(f"   ⚠️ Imagen intento {attempt}/{MAX_RETRIES} fallido: {e}")
                if attempt < MAX_RETRIES:
                    time.sleep(RETRY_DELAY * attempt)
                else:
                    log.error(f"❌ Error definitivo en imagen {image_url}: {e}")
                    return None

        return None

    def get_image_embeddings_batch(self, image_urls: List[str]) -> List[Optional[List[float]]]:
        resultados = []
        for url in image_urls:
            embedding = self.get_image_embedding(url)
            resultados.append(embedding)
            time.sleep(0.1)
        return resultados

    def cost_summary(self) -> Dict:
        costo_texto = self.tokens_texto * PRICE_PER_TOKEN_TEXTO
        costo_imagen = self.tokens_imagen * PRICE_PER_TOKEN_IMAGEN
        return {
            'tokens_texto': self.tokens_texto,
            'tokens_imagen': self.tokens_imagen,
            'costo_texto': costo_texto,
            'costo_imagen': costo_imagen,
            'costo_total': costo_texto + costo_imagen,
        }

# ─────────────────────────────────────────
# IMAGE QUALITY SELECTOR
# ─────────────────────────────────────────
class ImageQualitySelector:
    """Selecciona la mejor calidad de imagen disponible"""

    QUALITY_ORDER = [
        '', '-large', '-medium', '-home_default', '-small', '-cart_default'
    ]

    def __init__(self, base_url: str):
        self.base_url = base_url
        self.session = httpx.Client(timeout=5.0)

    def get_best_image_url(self, image_id: int) -> Optional[str]:
        id_str = str(image_id)
        path = '/'.join(list(id_str))

        for suffix in self.QUALITY_ORDER:
            url = f"{self.base_url}{path}/{id_str}{suffix}.jpg"
            try:
                response = self.session.head(url)
                if response.status_code == 200:
                    content_length = response.headers.get('content-length')
                    if content_length and int(content_length) > 5000:
                        return url
            except Exception:
                continue

        return None

# ─────────────────────────────────────────
# INDEXADOR UNIFICADO
# ─────────────────────────────────────────
class UnifiedIndexer:
    def __init__(self):
        self.embedder = OpenRouterEmbedder()
        self.quality_selector = ImageQualitySelector(BASE_IMG_URL)
        self.qdrant = QdrantClient(host='qdrant', port=6333)
        self.conn = None
        self.cursor = None

        self.image_stats = {
            'original': 0, 'large': 0, 'medium': 0,
            'thumbnail': 0, 'small': 0, 'error': 0
        }

        self.vector_size_imagen = self._probe_image_vector_size()
        self._ensure_collections()

    def _probe_image_vector_size(self) -> int:
        pixel_1x1_png = (
            "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR4nGNgAAIAAAUAAen63NgAAAAASUVORK5CYII="
        )
        try:
            resp = self.embedder.client.embeddings.create(
                model=MODELO_IMAGEN,
                input=[pixel_1x1_png],
                encoding_format="float"
            )
            dim = len(resp.data[0].embedding)
            log.info(f"🔎 Dimensión real detectada para '{MODELO_IMAGEN}': {dim}")
            return dim
        except Exception as e:
            log.warning(
                f"⚠️ No se pudo sondear la dimensión del modelo de imagen ({e}). "
                f"Usando fallback: {VECTOR_SIZE_IMAGEN_FALLBACK}"
            )
            return VECTOR_SIZE_IMAGEN_FALLBACK

    def _ensure_collections(self):
        try:
            self.qdrant.get_collection(COLLECTION_TEXTO)
            log.info(f"✅ Colección '{COLLECTION_TEXTO}' ya existe")
        except Exception:
            log.info(f"🆕 Creando colección '{COLLECTION_TEXTO}'...")
            self.qdrant.create_collection(
                collection_name=COLLECTION_TEXTO,
                vectors_config=VectorParams(size=VECTOR_SIZE_TEXTO, distance=Distance.COSINE)
            )
            log.info("✅ Colección creada")

        try:
            info = self.qdrant.get_collection(COLLECTION_IMAGENES)
            existing_size = info.config.params.vectors.size
            if existing_size != self.vector_size_imagen:
                log.error(
                    f"❌ La colección '{COLLECTION_IMAGENES}' existe con dimensión "
                    f"{existing_size}, pero el modelo '{MODELO_IMAGEN}' devuelve "
                    f"{self.vector_size_imagen}. Hay que borrar y recrear la colección."
                )
                raise SystemExit(1)
            log.info(f"✅ Colección '{COLLECTION_IMAGENES}' ya existe (dim {existing_size}, OK)")
        except SystemExit:
            raise
        except Exception:
            log.info(f"🆕 Creando colección '{COLLECTION_IMAGENES}' (dim {self.vector_size_imagen})...")
            self.qdrant.create_collection(
                collection_name=COLLECTION_IMAGENES,
                vectors_config=VectorParams(size=self.vector_size_imagen, distance=Distance.COSINE)
            )
            log.info("✅ Colección creada")

    def _connect_db(self):
        self.conn = pymysql.connect(
            host='db', port=3306,
            user='tum12607_webmas2', password='6060',
            database='tum12607_maracay',
            cursorclass=pymysql.cursors.DictCursor
        )
        self.cursor = self.conn.cursor()

    def _close_db(self):
        if self.cursor:
            self.cursor.close()
        if self.conn:
            self.conn.close()

    def _generar_point_id_imagen(self, stockid: str, image_id: int) -> int:
        base = f"{stockid}_{image_id}"
        return int(hashlib.md5(base.encode()).hexdigest()[:16], 16) % (2**63 - 1)

    def _registrar_calidad_imagen(self, url: str):
        if '-home_default' in url:
            self.image_stats['thumbnail'] += 1
        elif '-large' in url:
            self.image_stats['large'] += 1
        elif '-medium' in url:
            self.image_stats['medium'] += 1
        elif '-small' in url:
            self.image_stats['small'] += 1
        else:
            self.image_stats['original'] += 1

    # ────────────────────────────────────
    # OBTENER LISTA DE PRODUCTOS PARA BATCH
    # ────────────────────────────────────
    def obtener_productos_a_indexar(self, limit: int, offset: int = 0) -> List[str]:
        self._connect_db()
        try:
            self.cursor.execute(
                """
                SELECT stockid
                FROM stockmaster
                WHERE discontinued = 0
                ORDER BY stockid
                LIMIT %s OFFSET %s
                """,
                [limit, offset]
            )
            rows = self.cursor.fetchall()
            return [r['stockid'] for r in rows]
        finally:
            self._close_db()

    # ────────────────────────────────────
    # INDEXAR PRODUCTO INDIVIDUAL
    # ────────────────────────────────────
    def indexar_producto(self, stockid: str, force: bool = False):
        log.info("=" * 60)
        log.info(f"📦 INDEXANDO PRODUCTO: {stockid}")
        log.info(f"   Force: {force}")
        log.info("=" * 60)

        self._connect_db()

        try:
            self.cursor.execute("""
                SELECT 
                    s.stockid, s.description, s.longdescription,
                    GROUP_CONCAT(
                        DISTINCT CONCAT(m.modeldescrip, ' (', ma.marcadescrip, ')')
                        SEPARATOR ', '
                    ) as modelos_compatibles
                FROM stockmaster s
                LEFT JOIN stockmaster_modelo sm ON s.stockid = sm.stockid
                LEFT JOIN modelos m ON sm.idmodelo = m.idmodelo
                LEFT JOIN marcas ma ON m.idmarca = ma.idmarca
                WHERE s.stockid = %s AND s.discontinued = 0
                GROUP BY s.stockid
            """, [stockid])

            producto = self.cursor.fetchone()

            if not producto:
                log.error(f"❌ Producto {stockid} no encontrado")
                self._close_db()
                return

            log.info(f"✅ Producto: {producto['description']}")

            self.cursor.execute("""
                SELECT id_image, position, cover
                FROM stock_image
                WHERE id_product = %s
                ORDER BY position
            """, [stockid])

            imagenes = self.cursor.fetchall()
            log.info(f"📸 {len(imagenes)} imágenes")

            # ── INDEXAR TEXTO ──
            log.info("\n📝 Indexando texto...")
            raw = producto.get('description', '') or producto.get('longdescription', '') or ''
            desc = clean_description(raw)
            texto = normalizar_texto(desc[:MAX_TEXT_CHARS])

            try:
                embeddings = self.embedder.get_text_embeddings_batch([texto])
                point_id = sanitize_id(stockid)

                if force:
                    try:
                        self.qdrant.delete(collection_name=COLLECTION_TEXTO, points=[point_id])
                    except:
                        pass

                self.qdrant.upsert(
                    collection_name=COLLECTION_TEXTO,
                    points=[PointStruct(
                        id=point_id,
                        vector=embeddings[0],
                        payload={
                            'stockid_original': stockid,
                            'description': texto,
                            'description_original': producto.get('description', ''),
                            'modelos': producto.get('modelos_compatibles', ''),
                            'indexed_at': datetime.now().isoformat()
                        }
                    )]
                )
                log.info("   ✅ Texto indexado")
            except Exception as e:
                log.error(f"   ❌ Error: {e}")

            # ── INDEXAR IMÁGENES ──
            if imagenes:
                log.info(f"\n🖼️ Indexando {len(imagenes)} imágenes via OpenRouter...")

                puntos = []
                ok = 0

                for img in imagenes:
                    image_id = img['id_image']
                    point_id = self._generar_point_id_imagen(stockid, image_id)

                    best_url = self.quality_selector.get_best_image_url(image_id)

                    if not best_url:
                        log.warning(f"   ⚠️ Imagen {image_id} no encontrada")
                        continue

                    self._registrar_calidad_imagen(best_url)

                    if force:
                        try:
                            self.qdrant.delete(collection_name=COLLECTION_IMAGENES, points=[point_id])
                        except:
                            pass

                    log.info(f"   🖼️ Procesando imagen {image_id}...")
                    embedding = self.embedder.get_image_embedding(best_url)

                    if embedding is None:
                        log.warning(f"   ⚠️ Imagen {image_id} falló")
                        continue

                    puntos.append(PointStruct(
                        id=point_id,
                        vector=embedding,
                        payload={
                            'stockid_original': stockid,
                            'image_id': image_id,
                            'position': img['position'],
                            'cover': img['cover'] == 1,
                            'image_url': best_url,
                            'description': producto.get('description', ''),
                            'indexed_at': datetime.now().isoformat()
                        }
                    ))
                    ok += 1

                if puntos:
                    try:
                        self.qdrant.upsert(collection_name=COLLECTION_IMAGENES, points=puntos)
                        log.info(f"   ✅ {ok}/{len(imagenes)} imágenes indexadas")
                    except Exception as e:
                        log.error(f"   ❌ Error: {e}")
                else:
                    log.warning("   ⚠️ No se indexó ninguna imagen")

            log.info("\n" + "=" * 60)
            log.info(f"✅ PRODUCTO {stockid} INDEXADO")
            total = sum(v for k, v in self.image_stats.items() if k != 'error')
            if total > 0:
                log.info(f"   Imágenes: {total} (original: {self.image_stats['original']}, "
                        f"large: {self.image_stats['large']}, error: {self.image_stats['error']})")
            log.info("=" * 60)

        except Exception as e:
            log.error(f"❌ Error: {e}")
            import traceback
            log.error(traceback.format_exc())
        finally:
            self._close_db()

    # ────────────────────────────────────
    # INDEXAR EN LOTE
    # ────────────────────────────────────
    def indexar_batch(self, limit: int, offset: int = 0, force: bool = False):
        stockids = self.obtener_productos_a_indexar(limit, offset)
        log.info("#" * 60)
        log.info(f"🚀 INDEXANDO BATCH DE {len(stockids)} PRODUCTOS (offset={offset})")
        log.info("#" * 60)

        exitosos = 0
        fallidos = 0
        inicio = time.time()

        for i, stockid in enumerate(stockids, 1):
            log.info(f"\n▶️ [{i}/{len(stockids)}] {stockid}")
            try:
                self.indexar_producto(stockid, force)
                exitosos += 1
            except Exception as e:
                log.error(f"❌ Falló producto {stockid}: {e}")
                fallidos += 1

            # Resumen de costo parcial cada 10 productos
            if i % 10 == 0 or i == len(stockids):
                resumen = self.embedder.cost_summary()
                log.info(
                    f"   💰 Costo acumulado hasta ahora: "
                    f"${resumen['costo_total']:.4f} "
                    f"(texto ${resumen['costo_texto']:.4f} / imagen ${resumen['costo_imagen']:.4f})"
                )

        duracion = time.time() - inicio
        resumen = self.embedder.cost_summary()

        log.info("\n" + "#" * 60)
        log.info(f"🏁 BATCH TERMINADO")
        log.info(f"   Productos OK: {exitosos}   Fallidos: {fallidos}")
        log.info(f"   Duración: {duracion:.1f}s ({duracion/max(len(stockids),1):.2f}s/producto)")
        log.info(f"   Tokens texto:  {resumen['tokens_texto']:,}")
        log.info(f"   Tokens imagen: {resumen['tokens_imagen']:,}")
        log.info(f"   Costo texto:   ${resumen['costo_texto']:.4f}")
        log.info(f"   Costo imagen:  ${resumen['costo_imagen']:.4f}")
        log.info(f"   COSTO TOTAL:   ${resumen['costo_total']:.4f}")
        if exitosos > 0:
            log.info(f"   Costo promedio por producto: ${resumen['costo_total']/exitosos:.5f}")
        log.info("#" * 60)

# ─────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────
if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument('--producto', type=str, help='Producto individual a indexar')
    parser.add_argument('--batch', type=int, help='Indexar N productos (los primeros N no discontinuados)')
    parser.add_argument('--offset', type=int, default=0, help='Offset para --batch (para paginar)')
    parser.add_argument('--force', action='store_true', help='Forzar reindexación')

    args = parser.parse_args()

    if not args.producto and not args.batch:
        print("❌ Especificar --producto <id> o --batch <N>")
        print("Ejemplos:")
        print("  python indexador_unificado.py --producto 952-072")
        print("  python indexador_unificado.py --batch 100")
        print("  python indexador_unificado.py --batch 100 --offset 100  (siguiente tanda)")
        sys.exit(1)

    indexer = UnifiedIndexer()

    if args.batch:
        indexer.indexar_batch(args.batch, args.offset, args.force)
    else:
        indexer.indexar_producto(args.producto, args.force)