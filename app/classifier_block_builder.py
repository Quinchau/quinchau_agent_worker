# app/classifier_block_builder.py
"""
Construye y cachea el BLOQUE ESTÁTICO ÚNICO que consume el clasificador
unificado (intención + modelo + producto en una sola pasada LLM).

Reemplaza, para este consumidor específico, la doble lectura que hacía
el flujo viejo (catalog:terminos_patterns + catalog:herramientas en cada
mensaje) por un único registro derivado y pre-renderizado:

    classifier:static_block = {
        version, modelo_enum, diccionario_texto,
        intenciones_texto, built_at
    }

DISEÑO CLAVE — determinismo:
El texto final debe ser BYTE-IDÉNTICO entre reconstrucciones si los
datos fuente no cambiaron. Esto es un requisito, no un nice-to-have:
del prompt caching del proveedor (Anthropic/OpenRouter) depende que el
prefijo estático del prompt del clasificador no cambie turno a turno,
y ese prefijo se recorta de acá. Por eso:
  - se ordena SIEMPRE alfabéticamente (nunca por longitud/timestamp),
  - la 'version' es un hash del contenido fuente, no un timestamp,
  - no hay TTL corto forzando rebuild periódico sin motivo real (ver
    invalidar_bloque_estatico()).

DISEÑO CLAVE — formato del diccionario:
Texto compacto agrupado ("Termino (alias1, alias2)"), NO JSON. Medido
sobre el catálogo real (606 términos filtrados a modelo/producto):
JSON de objetos ≈ 9954 tokens vs. compacto ≈ 2526 tokens para el mismo
contenido. El JSON solo se usa donde es funcionalmente necesario (el
'tools'/response_format schema real que arma classifier.py), nunca
como texto de referencia dentro del prompt.
"""
import hashlib
import json
import logging
from datetime import datetime
from typing import Dict, List

from .catalog_cache import (
    catalog_cache,
    ENTIDADES_TERMINOS_VALIDAS,
    KEY_TERMINOS_PATTERNS,
    KEY_HERRAMIENTAS,
)
from .redis_queue import get_redis

logger = logging.getLogger(__name__)

KEY_STATIC_BLOCK = "classifier:static_block"
# Red de seguridad, no mecanismo primario de refresco (ver docstring del
# módulo). La invalidación real ocurre por cambio de 'version' via
# invalidar_bloque_estatico(), idealmente disparada por un trigger/hook
# al escribir en terminos_semanticos/terminos_alias/intenciones.
TTL_BACKSTOP = 1800  # 30 minutos


def _hash_catalogo(patterns: List[Dict], herramientas: List[Dict]) -> str:
    """
    Hash determinístico del contenido fuente. Sirve de 'version' — solo
    cambia cuando el catálogo cambió de verdad, no en cada rebuild por
    TTL. Se usa json.dumps con sort_keys para que el hash no dependa del
    orden en que Python recorrió las filas de MySQL.
    """
    payload = json.dumps(
        {"patterns": patterns, "herramientas": herramientas},
        sort_keys=True,
        default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _agrupar_por_termino(patterns: List[Dict]) -> Dict[str, Dict]:
    """
    Reagrupa la lista PLANA de patrones (una fila por término canónico +
    una fila por cada alias) en {termino_id: {termino, tipo, alias:[...]}}.

    catalog_cache.get_terminos_patterns() ya viene filtrado a
    modelo/producto por el INNER JOIN de origen; el filtro explícito acá
    (reusando ENTIDADES_TERMINOS_VALIDAS, la misma constante que usa el
    WHERE de la query en catalog_cache.py) es cinturón de seguridad
    adicional, no la única línea de defensa — y evita que en el futuro
    alguien actualice un lado del filtro y se olvide del otro.
    """
    por_termino: Dict[str, Dict] = {}
    for p in patterns:
        if p['entidad_nombre'] not in ENTIDADES_TERMINOS_VALIDAS:
            continue
        grupo = por_termino.setdefault(str(p['termino_id']), {
            'termino': p['termino'],
            'tipo': p['entidad_nombre'],
            'alias': [],
        })
        # La fila "canónica" tiene pattern == termino.lower(); el resto
        # son alias reales.
        if p['pattern'] != p['termino'].lower() and p['pattern'] not in grupo['alias']:
            grupo['alias'].append(p['pattern'])
    return por_termino


def _render_compacto(modelos: List[Dict], productos: List[Dict]) -> str:
    """
    Formato compacto agrupado por tipo. Una línea por término:
        Nombre (alias1, alias2, alias3)
    Sin alias, solo el nombre. Orden alfabético (determinístico).
    """
    def _lineas(items: List[Dict]) -> str:
        out = []
        for g in items:
            if g['alias']:
                out.append(f"{g['termino']} ({', '.join(g['alias'])})")
            else:
                out.append(g['termino'])
        return "\n".join(out)

    return (
        "MODELOS:\n" + _lineas(modelos) +
        "\n\nPRODUCTOS:\n" + _lineas(productos)
    )


def _render_intenciones(herramientas_raw: List[Dict]) -> str:
    """
    Texto de referencia de intenciones disponibles, a partir de
    catalog:herramientas (ya cacheado/con fallback a BD). Solo
    nombre + descripción — el detalle de entidades asociadas
    (bloqueante/orden_prioridad) NO se usa acá: esa lógica de
    aclaración vive explícitamente en cada handler (ver discusión de
    diseño — no se generaliza por entidad, la lógica difiere demasiado
    entre 'falta modelo' y 'falta producto' como para derivarla de una
    tabla genérica).
    """
    lineas = []
    for h in herramientas_raw:
        fn = h.get("function", {})
        nombre = fn.get("name", "")
        descripcion = fn.get("description", "")
        if not nombre:
            continue
        lineas.append(f"- {nombre}: {descripcion}")
    return "\n".join(lineas)


def construir_bloque_estatico() -> Dict:
    """
    Reconstruye el bloque estático a partir de las fuentes ya cacheadas
    (catalog_cache.get_terminos_patterns / get_herramientas). No abre
    conexión a MySQL por su cuenta — reusa el fallback Redis→BD que ya
    existe en esas dos capas.
    """
    patterns = catalog_cache.get_terminos_patterns()
    herramientas_raw = catalog_cache.get_herramientas()

    por_termino = _agrupar_por_termino(patterns)

    modelos = sorted(
        (g for g in por_termino.values() if g['tipo'] == 'modelo'),
        key=lambda g: g['termino'].lower(),
    )
    productos = sorted(
        (g for g in por_termino.values() if g['tipo'] == 'producto'),
        key=lambda g: g['termino'].lower(),
    )

    bloque = {
        'version': _hash_catalogo(patterns, herramientas_raw),
        'modelo_enum': [m['termino'] for m in modelos],
        'diccionario_texto': _render_compacto(modelos, productos),
        'intenciones_texto': _render_intenciones(herramientas_raw),
        'total_modelos': len(modelos),
        'total_productos': len(productos),
        'built_at': datetime.now().isoformat(),
    }

    # Nota: Se eleva a nivel INFO para poder auditar los bloques de texto generados en los logs de producción/worker.
    logger.info(
        f"🔍 [INSPECCIÓN PROMPT ESTÁTICO] Versión: {bloque['version']}\n"
        f"--- DICCIONARIO TEXTO ---\n{bloque['diccionario_texto']}\n"
        f"--- INTENCIONES TEXTO ---\n{bloque['intenciones_texto']}"
    )

    logger.info(
        f"🧱 Bloque estático reconstruido | version={bloque['version']} | "
        f"modelos={bloque['total_modelos']} | productos={bloque['total_productos']}"
    )
    return bloque


def get_bloque_estatico(forzar_rebuild: bool = False) -> Dict:
    """
    Punto de entrada para classifier.py. Cache-aside sobre
    classifier:static_block, con backstop de TTL largo (ver docstring
    del módulo sobre por qué no es TTL corto).
    """
    redis = get_redis()

    if not forzar_rebuild:
        try:
            cached = redis.get(KEY_STATIC_BLOCK)
            if cached:
                bloque = json.loads(cached)
                logger.debug(
                    f"📦 [DEBUG CACHE HIT] Bloque estático desde Redis | versión={bloque.get('version')} | "
                    f"built_at={bloque.get('built_at')}"
                )
                return bloque
        except Exception as e:
            logger.warning(f"⚠️ Redis no disponible para lectura (get bloque estático): {e}")

    # Nota: Asignación explícita de la variable 'bloque' cuando ocurre un cache miss, falla de Redis o rebuild forzado.
    bloque = construir_bloque_estatico()

    try:
        redis.setex(KEY_STATIC_BLOCK, TTL_BACKSTOP, json.dumps(bloque, default=str))
    except Exception as e:
        logger.warning(f"⚠️ No se pudo escribir cache de bloque estático: {e}")

    return bloque


def invalidar_bloque_estatico() -> Dict:
    """
    Invalidación explícita on-write. Llamar desde el punto donde se
    modifica terminos_semanticos / terminos_alias / intenciones /
    intencion_entidad (trigger de BD, hook de admin, o job periódico
    corto si no hay forma de enganchar el write directamente).

    IMPORTANTE: antes de reconstruir, borra también las keys de origen
    (catalog:terminos_patterns, catalog:herramientas). Sin este paso,
    un rebuild disparado justo después de modificar la BD podría seguir
    leyendo la versión cacheada de esas keys (TTL de hasta 5 min, ver
    CACHE_TTL en catalog_cache.py) y el bloque estático terminaría
    reconstruido con los mismos datos viejos — la invalidación quedaría
    sin efecto real hasta que esas keys venzan por su cuenta.

    Reconstruye y sobreescribe SIEMPRE — si el contenido fuente no
    cambió, 'version' da igual y el prefijo del prompt sigue siendo
    byte-idéntico (no se pierde el prompt caching del proveedor por
    haber llamado esto de más).
    """
    redis = get_redis()
    try:
        redis.delete(KEY_TERMINOS_PATTERNS, KEY_HERRAMIENTAS)
    except Exception as e:
        logger.warning(f"⚠️ No se pudo invalidar cache de origen (términos/herramientas): {e}")

    return get_bloque_estatico(forzar_rebuild=True)