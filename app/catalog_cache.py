# app/catalog_cache.py
import os
import json
import logging
import requests
from typing import List, Dict, Optional
from .database import get_db_cursor  # Se importa el context manager del pool
from .redis_queue import get_redis

logger = logging.getLogger(__name__)

CACHE_TTL = 300  # 5 minutos (config: intenciones, herramientas, términos)
CACHE_TTL_PRODUCTOS = 60  # 1 minuto (stock cambia más seguido que config)

KEY_INTENCIONES = "catalog:intenciones"
KEY_BLOQUEANTES = "catalog:bloqueantes"
KEY_TERMINOS_PATTERNS = "catalog:terminos_patterns"
KEY_HERRAMIENTAS = "catalog:herramientas"
KEY_PRODUCTOS_MODELO = "catalog:productos_modelo:{modelo}"
KEY_PRODUCTOS_TERMINO = "catalog:productos_termino:{termino}"
KEY_PRODUCTOS_MODELO_Y_TERMINO = "catalog:productos_modelo_y_termino:{modelo}:{termino}"
KEY_SYSTEM_FAQS = "catalog:system_faqs"

# URL base del API Node.js (desde CATALOG_URL_ENDPOINT)
CATALOG_URL_ENDPOINT = os.getenv("CATALOG_URL_ENDPOINT", "http://quinchau-api:3003/api/agent/catalog-url")
# Derivar productos-por-modelo de la misma base (reemplaza último segmento)
CATALOG_PRODUCTOS_MODELO_URL = CATALOG_URL_ENDPOINT.replace("catalog-url", "productos-por-modelo")
# Endpoint dual específico/transversal — busqueda_producto_generica con modelo ya en contexto
CATALOG_PRODUCTOS_MODELO_Y_TERMINO_URL = CATALOG_URL_ENDPOINT.replace("catalog-url", "productos-por-modelo-y-termino")
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
        # Se elimina self.db = get_db_connection() para evitar conexiones persistentes congeladas

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
            # Conexión tomada temporalmente del pool
            with get_db_cursor() as cursor:
                cursor.execute(query)
                return cursor.fetchall()
        except Exception as e:
            logger.error(f"❌ Error consultando intenciones: {e}")
            return []

    # ============================================
    # SYSTEM FAQS (preguntas generales del negocio)
    # ============================================

    def get_system_faqs(self):
        """Retorna [{id, question, answer}, ...] de FAQs activas del negocio."""
        try:
            cached = self.redis.get(KEY_SYSTEM_FAQS)
            if cached:
                return json.loads(cached)
        except Exception as e:
            logger.warning(f"⚠️ Redis no disponible (get system_faqs), usando BD directo: {e}")
            return self._load_system_faqs_from_db()

        data = self._load_system_faqs_from_db()
        try:
            self.redis.setex(KEY_SYSTEM_FAQS, CACHE_TTL, json.dumps(data, default=str))
        except Exception as e:
            logger.warning(f"⚠️ No se pudo escribir cache de system_faqs: {e}")
        return data

    def _load_system_faqs_from_db(self):
        query = """
        SELECT id, question, answer
        FROM system_faqs
        WHERE activo = 1
        ORDER BY sort_order
        """
        try:
            # Conexión tomada temporalmente del pool
            with get_db_cursor() as cursor:
                cursor.execute(query)
                return cursor.fetchall()
        except Exception as e:
            logger.error(f"❌ Error consultando system_faqs: {e}")
            return []

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
            # Cada bloque interactúa con el pool garantizando transacciones atómicas
            with get_db_cursor() as cursor:
                cursor.execute(query_intenciones)
                intenciones = cursor.fetchall()
        except Exception as e:
            logger.error(f"❌ Error consultando intenciones (herramientas): {e}")
            return []

        try:
            with get_db_cursor() as cursor:
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
            with get_db_cursor() as cursor:
                cursor.execute(query_terminos)
                results_terminos = cursor.fetchall()
        except Exception as e:
            logger.error(f"❌ Error en consulta SQL (términos): {e}")
            results_terminos = []

        try:
            with get_db_cursor() as cursor:
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
    # PRODUCTOS POR MODELO — O POR TÉRMINO SI NO HAY MODELO
    #     Redis (TTL corto, stock variable) → Node (fuente de verdad) → Redis
    # ============================================

    def get_productos_por_modelo(self, modelo: Optional[str] = None, producto: Optional[str] = None) -> List[Dict]:
        """
        Catálogo completo de productos de un modelo (sin filtro de texto),
        usado para poblar el enum del tool call, y por busqueda_producto_modelo
        para que el LLM razone semánticamente sobre TODO el catálogo del modelo.

        Si `modelo` no viene pero sí `producto`, busca en su lugar por
        término libre sobre la descripción del producto (ej. "caucho
        90-90-18") — para el caso de productos que no se identifican por
        modelo de moto sino por un atributo propio (medida, marca, etc.).
        Cuando ambos vienen, `modelo` es el filtro primario (comportamiento
        sin cambios); `producto` solo se usa cuando `modelo` está ausente.

        NOTA: para busqueda_producto_generica CON modelo ya en contexto,
        usar get_productos_modelo_y_termino en su lugar — este método NO
        filtra por término cuando hay modelo, siempre trae el catálogo
        completo (por diseño, para busqueda_producto_modelo).

        Retorna SIEMPRE una lista (vacía en caso de error), nunca None —
        así el resto del pipeline no necesita chequear None en cada uso.
        """
        if not modelo and not producto:
            return []

        if modelo:
            cache_key = KEY_PRODUCTOS_MODELO.format(modelo=modelo.lower().strip())
        else:
            cache_key = KEY_PRODUCTOS_TERMINO.format(termino=producto.lower().strip())

        try:
            cached = self.redis.get(cache_key)
            if cached:
                productos = json.loads(cached)
                logger.info(f"📦 Cache hit productos de '{modelo or producto}' ({len(productos)})")
                return productos
        except Exception as e:
            logger.warning(f"⚠️ Redis no disponible (productos por modelo/término): {e}")
            return self._fetch_productos_por_modelo_backend(modelo, producto)

        productos = self._fetch_productos_por_modelo_backend(modelo, producto)
        try:
            self.redis.setex(cache_key, CACHE_TTL_PRODUCTOS, json.dumps(productos, default=str))
        except Exception as e:
            logger.warning(f"⚠️ No se pudo escribir cache de productos por modelo/término: {e}")
        return productos

    def _fetch_productos_por_modelo_backend(self, modelo: Optional[str], producto: Optional[str] = None) -> List[Dict]:
        """
        Llama al endpoint de Node (agent-resolver.service) que devuelve
        TODOS los productos del modelo, sin filtro de texto — o, cuando
        no hay modelo, todos los productos que matcheen `producto` como
        término de búsqueda libre.
        """
        url = CATALOG_PRODUCTOS_MODELO_URL
        payload = {}
        if modelo:
            payload["identidad_modelo"] = modelo
        if producto:
            payload["producto"] = producto

        etiqueta = modelo or producto

        try:
            response = requests.post(
                url,
                json=payload,
                headers={"Content-Type": "application/json"},
                timeout=NODE_AGENT_TIMEOUT,
            )
            response.raise_for_status()
            data = response.json()

            if not data.get("success"):
                logger.warning(f"⚠️ Node respondió error para '{etiqueta}': {data.get('error')}")
                return []

            productos = data.get("data", {}).get("productos", [])
            logger.info(f"✅ Catálogo de '{etiqueta}' obtenido de Node: {len(productos)} productos")
            return productos

        except requests.exceptions.Timeout:
            logger.warning(f"⚠️ Timeout consultando Node (productos por modelo/término) para '{etiqueta}'")
            return []
        except requests.exceptions.ConnectionError:
            logger.warning(f"⚠️ Error de conexión con Node (productos por modelo/término) para '{etiqueta}'")
            return []
        except Exception as e:
            logger.error(f"❌ Error consultando Node (productos por modelo/término) para '{etiqueta}': {e}")
            return []

    # ============================================
    # PRODUCTOS POR MODELO + TÉRMINO (dual: específico + transversal)
    #     Exclusivo de busqueda_producto_generica con modelo YA en contexto
    #     Redis (TTL corto) → Node (endpoint dual) → Redis
    # ============================================

    def get_productos_modelo_y_termino(self, modelo: str, producto: str) -> Dict:
        """
        Usado exclusivamente por busqueda_producto_generica cuando ya hay
        un modelo definido en el state de conversación Y el mensaje actual
        trae además un término de producto (ej. "tienen baterías?" con
        modelo=HJ125S ya seteado).

        A diferencia de get_productos_por_modelo (catálogo completo sin
        filtrar), este método SÍ filtra por término y devuelve DOS listas
        ya resueltas por Node sin solapamiento de ids:

        - 'especifico': productos del modelo que matchean el término.
        - 'transversal': productos de TODO el catálogo que matchean el
          término, excluyendo los ids ya presentes en 'especifico'.

        Retorna SIEMPRE un dict con ambas listas (vacías en caso de
        error), nunca None:
            {'especifico': [...], 'transversal': [...]}
        """
        if not modelo or not producto:
            return {'especifico': [], 'transversal': []}

        cache_key = KEY_PRODUCTOS_MODELO_Y_TERMINO.format(
            modelo=modelo.lower().strip(),
            termino=producto.lower().strip(),
        )

        try:
            cached = self.redis.get(cache_key)
            if cached:
                resultado = json.loads(cached)
                logger.info(
                    f"📦 Cache hit productos modelo+término '{modelo}'+'{producto}' "
                    f"(especifico={len(resultado.get('especifico', []))}, "
                    f"transversal={len(resultado.get('transversal', []))})"
                )
                return resultado
        except Exception as e:
            logger.warning(f"⚠️ Redis no disponible (productos modelo+término): {e}")
            return self._fetch_productos_modelo_y_termino_backend(modelo, producto)

        resultado = self._fetch_productos_modelo_y_termino_backend(modelo, producto)
        try:
            self.redis.setex(cache_key, CACHE_TTL_PRODUCTOS, json.dumps(resultado, default=str))
        except Exception as e:
            logger.warning(f"⚠️ No se pudo escribir cache de productos modelo+término: {e}")
        return resultado

    def _fetch_productos_modelo_y_termino_backend(self, modelo: str, producto: str) -> Dict:
        """
        Llama al endpoint dual de Node (productos-por-modelo-y-termino)
        que devuelve 'especifico' y 'transversal' ya separados y sin
        ids duplicados entre ambas listas.
        """
        try:
            response = requests.post(
                CATALOG_PRODUCTOS_MODELO_Y_TERMINO_URL,
                json={"identidad_modelo": modelo, "producto": producto},
                headers={"Content-Type": "application/json"},
                timeout=NODE_AGENT_TIMEOUT,
            )
            response.raise_for_status()
            data = response.json()

            if not data.get("success"):
                logger.warning(
                    f"⚠️ Node respondió error para modelo+término '{modelo}'+'{producto}': {data.get('error')}"
                )
                return {'especifico': [], 'transversal': []}

            payload = data.get("data", {})
            especifico = payload.get("especifico", [])
            transversal = payload.get("transversal", [])

            logger.info(
                f"✅ Catálogo dual '{modelo}'+'{producto}' obtenido de Node: "
                f"especifico={len(especifico)}, transversal={len(transversal)}"
            )
            return {'especifico': especifico, 'transversal': transversal}

        except requests.exceptions.Timeout:
            logger.warning(f"⚠️ Timeout consultando Node (modelo+término) para '{modelo}'+'{producto}'")
            return {'especifico': [], 'transversal': []}
        except requests.exceptions.ConnectionError:
            logger.warning(f"⚠️ Error de conexión con Node (modelo+término) para '{modelo}'+'{producto}'")
            return {'especifico': [], 'transversal': []}
        except Exception as e:
            logger.error(f"❌ Error consultando Node (modelo+término) para '{modelo}'+'{producto}': {e}")
            return {'especifico': [], 'transversal': []}

# ============================================
# INSTANCIA GLOBAL PARA IMPORTACIÓN
# ============================================
catalog_cache = CatalogCache()