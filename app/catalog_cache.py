# app/catalog_cache.py
import os
import json
import logging
import requests
from typing import List, Dict, Optional
from .database import get_db_connection
from .redis_queue import get_redis

logger = logging.getLogger(__name__)

CACHE_TTL = 300  # 5 minutos (config: intenciones, herramientas, términos)
CACHE_TTL_PRODUCTOS = 60  # 1 minuto (stock cambia más seguido que config)

KEY_INTENCIONES = "catalog:intenciones"
KEY_BLOQUEANTES = "catalog:bloqueantes"
KEY_TERMINOS_PATTERNS = "catalog:terminos_patterns"
KEY_HERRAMIENTAS = "catalog:herramientas"
KEY_PRODUCTOS_MODELO = "catalog:productos_modelo:{modelo}"

# URL base del API Node.js (desde CATALOG_URL_ENDPOINT)
CATALOG_URL_ENDPOINT = os.getenv("CATALOG_URL_ENDPOINT", "http://quinchau-api:3003/api/agent/catalog-url")
# Derivar productos-por-modelo de la misma base (reemplaza último segmento)
CATALOG_PRODUCTOS_MODELO_URL = CATALOG_URL_ENDPOINT.replace("catalog-url", "productos-por-modelo")
NODE_AGENT_TIMEOUT = 5  # segundos

# JSON Schema no tiene tipos nativos de fecha/hora: se mapean a string + format
TIPO_MAP = {
    "string": "string",
    "integer": "integer",
    "decimal": "number",
    "boolean": "boolean",
    "date": "string",
    "time": "string",
    "array": "array"
}
FORMATO_EXTRA = {
    "date": {"format": "date"},
    "time": {"format": "time"},
}

# sin_clasificar SE INCLUYE como tool explícita (no se excluye).
EXCLUIR_DE_HERRAMIENTAS = set()


class CatalogCache:
    def __init__(self):
        self.redis = get_redis()
        self.db = get_db_connection()

    # ============================================
    # INTENCIONES
    # ============================================

    def get_intenciones(self):
        try:
            cached = self.redis.get(KEY_INTENCIONES)
            if cached:
                return json.loads(cached)
        except Exception as e:
            logger.warning(f"⚠️ Redis no disponible (get intenciones), usando BD directo: {e}")
            return self._load_intenciones_from_db()

        data = self._load_intenciones_from_db()
        try:
            self.redis.setex(KEY_INTENCIONES, CACHE_TTL, json.dumps(data, default=str))
        except Exception as e:
            logger.warning(f"⚠️ No se pudo escribir cache de intenciones: {e}")
        return data

    def _load_intenciones_from_db(self):
        query = """
        SELECT nombre, descripcion
        FROM intenciones
        WHERE activo = 1
        ORDER BY id
        """
        try:
            with self.db.cursor() as cursor:
                cursor.execute(query)
                return cursor.fetchall()
        except Exception as e:
            logger.error(f"❌ Error consultando intenciones: {e}")
            return []

    # ============================================
    # ENTIDADES BLOQUEANTES POR INTENCIÓN
    # ============================================

    def get_bloqueantes_map(self):
        """Dict {intencion_nombre: [entidad1, entidad2, ...]}"""
        try:
            cached = self.redis.get(KEY_BLOQUEANTES)
            if cached:
                return json.loads(cached)
        except Exception as e:
            logger.warning(f"⚠️ Redis no disponible (get bloqueantes), usando BD directo: {e}")
            return self._load_bloqueantes_from_db()

        data = self._load_bloqueantes_from_db()
        try:
            self.redis.setex(KEY_BLOQUEANTES, CACHE_TTL, json.dumps(data, default=str))
        except Exception as e:
            logger.warning(f"⚠️ No se pudo escribir cache de bloqueantes: {e}")
        return data

    def _load_bloqueantes_from_db(self):
        query = """
        SELECT i.nombre as intencion, e.nombre as entidad
        FROM intenciones i
        JOIN intencion_entidad ie ON i.id = ie.id_intencion
        JOIN entidades e ON ie.id_entidad = e.id
        WHERE ie.bloqueante = 1 AND i.activo = 1
        ORDER BY i.nombre, ie.orden_prioridad
        """
        try:
            with self.db.cursor() as cursor:
                cursor.execute(query)
                rows = cursor.fetchall()
        except Exception as e:
            logger.error(f"❌ Error consultando entidades bloqueantes: {e}")
            return {}

        result = {}
        for row in rows:
            result.setdefault(row['intencion'], []).append(row['entidad'])
        return result

    # ============================================
    # HERRAMIENTAS (tools) PARA TOOL CALLING
    # ============================================

    def get_herramientas(self):
        try:
            cached = self.redis.get(KEY_HERRAMIENTAS)
            if cached:
                return json.loads(cached)
        except Exception as e:
            logger.warning(f"⚠️ Redis no disponible (get herramientas), usando BD directo: {e}")
            return self._load_herramientas_from_db()

        data = self._load_herramientas_from_db()
        try:
            self.redis.setex(KEY_HERRAMIENTAS, CACHE_TTL, json.dumps(data, default=str))
        except Exception as e:
            logger.warning(f"⚠️ No se pudo escribir cache de herramientas: {e}")
        return data

    def _load_herramientas_from_db(self):
        query_intenciones = """
        SELECT id, nombre, descripcion
        FROM intenciones
        WHERE activo = 1
        ORDER BY id
        """
        query_relaciones = """
        SELECT
            ie.id_intencion,
            e.nombre as entidad_nombre,
            e.descripcion as entidad_descripcion,
            e.tipo as entidad_tipo,
            ie.bloqueante,
            ie.orden_prioridad
        FROM intencion_entidad ie
        JOIN entidades e ON ie.id_entidad = e.id
        JOIN intenciones i ON ie.id_intencion = i.id
        WHERE i.activo = 1
        ORDER BY ie.id_intencion, ie.orden_prioridad
        """

        try:
            with self.db.cursor() as cursor:
                cursor.execute(query_intenciones)
                intenciones = cursor.fetchall()
        except Exception as e:
            logger.error(f"❌ Error consultando intenciones (herramientas): {e}")
            return []

        try:
            with self.db.cursor() as cursor:
                cursor.execute(query_relaciones)
                relaciones = cursor.fetchall()
        except Exception as e:
            logger.error(f"❌ Error consultando intencion_entidad (herramientas): {e}")
            relaciones = []

        rel_por_intencion = {}
        for row in relaciones:
            rel_por_intencion.setdefault(row['id_intencion'], []).append(row)

        herramientas = []
        for intencion in intenciones:
            if intencion['nombre'] in EXCLUIR_DE_HERRAMIENTAS:
                continue

            properties = {}
            required = []
            for rel in rel_por_intencion.get(intencion['id'], []):
                tipo_sql = rel['entidad_tipo']
                prop = {
                    "type": TIPO_MAP.get(tipo_sql, "string"),
                    "description": rel['entidad_descripcion'] or rel['entidad_nombre'],
                }
                prop.update(FORMATO_EXTRA.get(tipo_sql, {}))
                properties[rel['entidad_nombre']] = prop
                if rel['bloqueante']:
                    required.append(rel['entidad_nombre'])

            descripcion = intencion['descripcion'] or intencion['nombre']

            herramientas.append({
                "type": "function",
                "function": {
                    "name": intencion['nombre'],
                    "description": descripcion,
                    "parameters": {
                        "type": "object",
                        "properties": properties,
                        "required": required,
                    },
                },
            })

        return herramientas

    # ============================================
    # TÉRMINOS + ALIAS (merge + dedup + sort ya resuelto)
    # ============================================

    def get_terminos_patterns(self):
        try:
            cached = self.redis.get(KEY_TERMINOS_PATTERNS)
            if cached:
                return json.loads(cached)
        except Exception as e:
            logger.warning(f"⚠️ Redis no disponible (get terminos), usando BD directo: {e}")
            return self._load_terminos_patterns_from_db()

        data = self._load_terminos_patterns_from_db()
        try:
            self.redis.setex(KEY_TERMINOS_PATTERNS, CACHE_TTL, json.dumps(data, default=str))
        except Exception as e:
            logger.warning(f"⚠️ No se pudo escribir cache de términos: {e}")
        return data

    def _load_terminos_patterns_from_db(self):
        query_terminos = """
        SELECT
            ts.id as termino_id,
            ts.termino,
            ts.id_entidad,
            e.nombre as entidad_nombre,
            ts.termino as pattern
        FROM terminos_semanticos ts
        LEFT JOIN entidades e ON ts.id_entidad = e.id
        WHERE ts.activo = 1
        """
        query_alias = """
        SELECT
            ts.id as termino_id,
            ts.termino,
            ts.id_entidad,
            e.nombre as entidad_nombre,
            ta.alias as pattern
        FROM terminos_semanticos ts
        JOIN terminos_alias ta ON ts.id = ta.id_termino
        LEFT JOIN entidades e ON ts.id_entidad = e.id
        WHERE ts.activo = 1
        ORDER BY LENGTH(ta.alias) DESC
        """

        try:
            with self.db.cursor() as cursor:
                cursor.execute(query_terminos)
                results_terminos = cursor.fetchall()
        except Exception as e:
            logger.error(f"❌ Error en consulta SQL (términos): {e}")
            results_terminos = []

        try:
            with self.db.cursor() as cursor:
                cursor.execute(query_alias)
                results_alias = cursor.fetchall()
        except Exception as e:
            logger.error(f"❌ Error en consulta SQL (alias): {e}")
            results_alias = []

        all_patterns = []
        seen_patterns = set()

        for row in results_terminos:
            pattern = row['pattern'].lower()
            if pattern not in seen_patterns:
                seen_patterns.add(pattern)
                all_patterns.append({
                    'termino_id': row['termino_id'],
                    'termino': row['termino'],
                    'id_entidad': row['id_entidad'],
                    'entidad_nombre': row['entidad_nombre'] or 'no_clasificado',
                    'pattern': pattern
                })

        for row in results_alias:
            pattern = row['pattern'].lower()
            if pattern not in seen_patterns:
                seen_patterns.add(pattern)
                all_patterns.append({
                    'termino_id': row['termino_id'],
                    'termino': row['termino'],
                    'id_entidad': row['id_entidad'],
                    'entidad_nombre': row['entidad_nombre'] or 'no_clasificado',
                    'pattern': pattern
                })

        all_patterns.sort(key=lambda x: len(x['pattern']), reverse=True)
        return all_patterns

    # ============================================
    # PRODUCTOS POR MODELO (nuevo)
    #    Redis (TTL corto, stock variable) → Node (fuente de verdad) → Redis
    # ============================================

    def get_productos_por_modelo(self, modelo: str) -> List[Dict]:
        """
        Catálogo completo de productos de un modelo (sin filtro de texto),
        usado para poblar el enum del tool call.

        Retorna SIEMPRE una lista (vacía en caso de error), nunca None —
        así el resto del pipeline no necesita chequear None en cada uso.
        """
        if not modelo:
            return []

        modelo_normalizado = modelo.lower().strip()
        cache_key = KEY_PRODUCTOS_MODELO.format(modelo=modelo_normalizado)

        try:
            cached = self.redis.get(cache_key)
            if cached:
                productos = json.loads(cached)
                logger.info(f"📦 Cache hit productos de '{modelo}' ({len(productos)})")
                return productos
        except Exception as e:
            logger.warning(f"⚠️ Redis no disponible (productos por modelo): {e}")
            return self._fetch_productos_por_modelo_backend(modelo)

        productos = self._fetch_productos_por_modelo_backend(modelo)
        try:
            self.redis.setex(cache_key, CACHE_TTL_PRODUCTOS, json.dumps(productos, default=str))
        except Exception as e:
            logger.warning(f"⚠️ No se pudo escribir cache de productos por modelo: {e}")
        return productos

    def _fetch_productos_por_modelo_backend(self, modelo: str) -> List[Dict]:
        """
        Llama al endpoint de Node (agent-resolver.service) que devuelve
        TODOS los productos del modelo, sin filtro de texto.
        """
        # Usar CATALOG_PRODUCTOS_MODELO_URL que ya está definida al inicio del archivo
        url = CATALOG_PRODUCTOS_MODELO_URL
        try:
            response = requests.post(
                url,
                json={"identidad_modelo": modelo},
                headers={"Content-Type": "application/json"},
                timeout=NODE_AGENT_TIMEOUT,
            )
            response.raise_for_status()
            data = response.json()

            if not data.get("success"):
                logger.warning(f"⚠️ Node respondió error para '{modelo}': {data.get('error')}")
                return []

            productos = data.get("data", {}).get("productos", [])
            logger.info(f"✅ Catálogo de '{modelo}' obtenido de Node: {len(productos)} productos")
            return productos

        except requests.exceptions.Timeout:
            logger.warning(f"⚠️ Timeout consultando Node (productos por modelo) para '{modelo}'")
            return []
        except requests.exceptions.ConnectionError:
            logger.warning(f"⚠️ Error de conexión con Node (productos por modelo) para '{modelo}'")
            return []
        except Exception as e:
            logger.error(f"❌ Error consultando Node (productos por modelo) para '{modelo}': {e}")
            return []

# ============================================
# INSTANCIA GLOBAL PARA IMPORTACIÓN
# ============================================
catalog_cache = CatalogCache()