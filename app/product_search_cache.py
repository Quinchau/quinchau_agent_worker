"""
Caché compartida de resultados de catálogo (producto + modelo) → lista de
candidatos crudos del backend.

Esta caché NO está atada a contact_id: si el cliente A pregunta por
"platinera jaguar" y dos minutos después el cliente B pregunta lo mismo,
se reusa el mismo resultado sin volver a pegarle al backend.

El estado por conversación (qué está viendo CADA cliente, cuál seleccionó)
sigue viviendo en AgentStateManager / Redis por contact_id, como hasta ahora.
Este módulo es solo la capa de "resultado de búsqueda", independiente de quién
pregunta.
"""
import json
import logging
import os
import re
import unicodedata
from datetime import datetime

import httpx

# Reusamos la misma factory de conexión que ya usa app/catalog_cache.py
# (la caché de metadata: intenciones/herramientas/términos), en vez de abrir
# una conexión Redis nueva. Mantiene una sola pool de conexiones en el proceso.
from .redis_queue import get_redis

logger = logging.getLogger(__name__)

CATALOG_ENDPOINT = os.getenv(
    "CATALOG_ENDPOINT", "http://backend:8000/products/internal/resolve-by-entities"
)
CATALOG_TIMEOUT = float(os.getenv("CATALOG_TIMEOUT", "3.0"))

# TTL de la caché de resultados de BÚSQUEDA de productos. Es un dominio
# distinto al de catalog_cache.py (que cachea metadata de configuración con
# invalidación manual desde el panel admin) — acá el TTL es la única forma
# de invalidación, porque las keys son dinámicas (una por cada producto+modelo
# consultado), no un puñado fijo de keys.
CATALOG_CACHE_TTL = int(os.getenv("PRODUCT_SEARCH_CACHE_TTL_SECONDS", "1800"))  # 30 min


def _normalizar(texto: str) -> str:
    """
    Normaliza producto/modelo para que la key sea estable:
    minúsculas, sin acentos, sin espacios extra.
    'Platinera ' -> 'platinera' | 'GN 125' -> 'gn 125' -> colapsa espacios
    """
    if not texto:
        return ""
    texto = texto.strip().lower()
    texto = unicodedata.normalize("NFKD", texto).encode("ascii", "ignore").decode("ascii")
    texto = re.sub(r"\s+", " ", texto)
    return texto


def _build_key(producto: str, modelo: str) -> str:
    return f"catalogo:busqueda:{_normalizar(producto)}:{_normalizar(modelo)}"


def get_cached(producto: str, modelo: str) -> dict | None:
    """
    Devuelve el dict cacheado {producto, modelo, resultados, url_catalogo,
    timestamp} si existe y no venció, o None si no hay caché.
    """
    key = _build_key(producto, modelo)
    try:
        r = get_redis()
        raw = r.get(key)
        if not raw:
            logger.info(f"🗄️ Caché MISS: {key}")
            return None
        logger.info(f"🗄️ Caché HIT: {key}")
        return json.loads(raw)
    except Exception as e:
        logger.error(f"❌ Error leyendo caché de catálogo ({key}): {e}")
        return None  # si redis falla, degradamos a "sin caché", no rompemos el flujo


def set_cached(producto: str, modelo: str, resultados: list, url_catalogo: str | None = None) -> None:
    """
    Guarda en caché la respuesta cruda del backend para (producto, modelo).
    """
    key = _build_key(producto, modelo)
    payload = {
        "producto": producto,
        "modelo": modelo,
        "resultados": resultados,
        "url_catalogo": url_catalogo,
        "timestamp": datetime.now().isoformat(),
    }
    try:
        r = get_redis()
        r.set(key, json.dumps(payload), ex=CATALOG_CACHE_TTL)
        logger.info(f"🗄️ Caché SET: {key} ({len(resultados)} resultados, TTL={CATALOG_CACHE_TTL}s)")
    except Exception as e:
        logger.error(f"❌ Error escribiendo caché de catálogo ({key}): {e}")
        # no relanzamos: si falla el cacheo, seguimos con el flujo normal


def invalidar(producto: str, modelo: str) -> None:
    """
    Borra manualmente una entrada de caché (ej: si se detecta que el stock
    cambió, o para forzar un refresh).
    """
    key = _build_key(producto, modelo)
    try:
        r = get_redis()
        r.delete(key)
        logger.info(f"🗑️ Caché invalidada manualmente: {key}")
    except Exception as e:
        logger.error(f"❌ Error invalidando caché de catálogo ({key}): {e}")


def obtener_candidatos(producto: str, modelo: str, forzar_refresh: bool = False) -> dict:
    """
    Punto de entrada único: primero intenta la caché, si no hay (o
    forzar_refresh=True) pega al backend y cachea el resultado.

    Devuelve siempre el mismo shape:
    {
        "resultados": [...],   # lista cruda tal como la devuelve el backend
        "url_catalogo": str|None,
        "from_cache": bool,
    }
    """
    if not forzar_refresh:
        cached = get_cached(producto, modelo)
        if cached is not None:
            return {
                "resultados": cached.get("resultados", []),
                "url_catalogo": cached.get("url_catalogo"),
                "from_cache": True,
            }

    logger.info(f"🌐 Consultando backend: producto='{producto}', modelo='{modelo}'")
    with httpx.Client(timeout=CATALOG_TIMEOUT) as client:
        response = client.post(
            CATALOG_ENDPOINT,
            json={"identidad_producto": producto, "identidad_modelo": modelo},
        )
        response.raise_for_status()
        data = response.json()

    resultados = data.get("results", [])
    url_catalogo = data.get("url")

    # Solo cacheamos si vino algo útil (evita cachear "vacío" por un typo puntual)
    if resultados:
        set_cached(producto, modelo, resultados, url_catalogo)

    return {
        "resultados": resultados,
        "url_catalogo": url_catalogo,
        "from_cache": False,
    }