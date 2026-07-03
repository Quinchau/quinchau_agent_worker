# app/catalog_cache.py
import json
import logging
from .database import get_db_connection
from .redis_queue import get_redis

logger = logging.getLogger(__name__)

CACHE_TTL = 300  # 5 minutos

KEY_INTENCIONES = "catalog:intenciones"
KEY_BLOQUEANTES = "catalog:bloqueantes"
KEY_TERMINOS_PATTERNS = "catalog:terminos_patterns"
KEY_HERRAMIENTAS = "catalog:herramientas"

# JSON Schema no tiene tipos nativos de fecha/hora: se mapean a string + format
TIPO_MAP = {
    "string": "string",
    "integer": "integer",
    "decimal": "number",
    "boolean": "boolean",
    "date": "string",
    "time": "string",
}
FORMATO_EXTRA = {
    "date": {"format": "date"},
    "time": {"format": "time"},
}

# sin_clasificar SE INCLUYE como tool explícita (no se excluye).
# Motivo: con tool_choice="required" el modelo siempre debe elegir algo, y
# necesitamos que "no reconozco esto" sea una elección afirmativa y
# reproducible, no lo que pasa por default cuando el modelo no llama
# ninguna tool. Ver discusión: casos "chirulai"/"chuflin"/"chinchulin"
# donde el mismo tipo de mensaje daba resultados distintos entre sí.
EXCLUIR_DE_HERRAMIENTAS = set()

DESCRIPCION_SIN_CLASIFICAR = (
    "Usar cuando el mensaje del cliente menciona un producto o repuesto que "
    "no se reconoce por nombre ni por sinónimo/jerga conocida, o cuando el "
    "mensaje es genuinamente ambiguo y ninguna otra herramienta aplica. "
    "Esto deriva la conversación a un agente humano."
)


class CatalogCache:
    def __init__(self):
        self.redis = get_redis()
        self.db = get_db_connection()

    # ============================================
    # INTENCIONES
    # ============================================

    def get_intenciones(self):
        """Lista de intenciones activas: [{'nombre':..., 'descripcion':...}, ...]"""
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
    #    intenciones + entidades + intencion_entidad -> JSON schema
    # ============================================

    def get_herramientas(self):
        """Lista de tools en formato OpenAI function-calling, generadas desde
        las mismas tablas que ya usás para intenciones/entidades bloqueantes.
        [{'type': 'function', 'function': {'name':..., 'description':..., 'parameters': {...}}}, ...]
        """
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

            if intencion['nombre'] == 'sin_clasificar':
                descripcion = DESCRIPCION_SIN_CLASIFICAR
            else:
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
        """Lista ya combinada/deduplicada/ordenada por longitud DESC, lista para matching"""
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


# TODO: cuando se retome la invalidación manual, los endpoints de edición
# de términos/alias deberán llamar a algo como:
#   CatalogCache().redis.delete(KEY_TERMINOS_PATTERNS)
# justo después de un UPDATE/INSERT exitoso en terminos_semanticos o terminos_alias.
#
# Lo mismo aplica para KEY_HERRAMIENTAS: si editás intenciones, entidades o
# intencion_entidad desde el panel de administración, hay que invalidar
# también catalog:herramientas (y catalog:intenciones / catalog:bloqueantes,
# que quedan redundantes una vez migrado el dispatcher a tool calling).