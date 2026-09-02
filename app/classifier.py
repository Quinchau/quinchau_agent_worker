# app/classifier.py
"""
Clasificador unificado: intención + modelo + producto en una sola pasada LLM.

Reemplaza el flujo viejo que tenía dos sistemas separados:
  - Gate 2.5 / 2.7 (entity_resolver.py): matching léxico pre-LLM.
  - LLM de intención (catalog:herramientas): tool-calling para intención,
    pero sin resolver modelo/producto en la misma llamada.

Ahora es una sola llamada que resuelve los tres en paralelo. El LLM recibe
el bloque estático cacheado (intenciones + diccionario de términos) y el
bloque dinámico (historial + modelo heredado + mensaje actual). La
validación determinística del resultado (exact-match contra el set de
términos canónicos) ocurre en tasks.py, no acá.

DISEÑO — tool-calling con intenciones como funciones:
Cada intención activa del catálogo es una función en el `tools` schema.
El LLM debe elegir exactamente una. Dentro de la función elegida:
  - 'modelo': enum cerrado de los 119 valores canónicos del catálogo
    (obtenidos de bloque['modelo_enum']). Si no corresponde, null.
  - 'producto': array de strings libres (sin enum — 484 términos hacen
    el enum contraproducente; la validación post-hoc en tasks.py filtra
    lo que no matchee exacto contra el catálogo).
  - resto de entidades de esa intención: pasadas tal cual según su
    propia definición en catalog_cache.get_herramientas().

DISEÑO — caching del prefijo estático:
OpenRouter con prefijo caching automático. Se requiere que el mensaje 'system'
sea 100% estático e idéntico byte a byte. Los datos del turno dinámico
(historial/modelo_heredado/mensaje) viajan exclusivamente en el rol 'user'.
"""
import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from openai import OpenAI

from .catalog_cache import catalog_cache
from .classifier_block_builder import get_bloque_estatico

logger = logging.getLogger(__name__)

OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
CLASSIFIER_MODEL = os.getenv("CLASSIFIER_MODEL", "qwen/qwen-2.5-72b-instruct")
CLASSIFIER_TEMPERATURE = 0.0  # clasificación determinística, sin varianza
CLASSIFIER_MAX_TOKENS = 256   # tool call de una intención + 3-4 entidades

_PROMPT_PATH = Path(__file__).parent / "contextos" / "prompt_clasificador_unificado.txt"


def _load_prompt_template() -> str:
    """Carga el template del sistema desde disco. Se llama una vez y se cachea en módulo."""
    return _PROMPT_PATH.read_text(encoding="utf-8")


# Cacheado a nivel de módulo — el archivo no cambia en runtime.
_PROMPT_TEMPLATE: str = _load_prompt_template()


def _build_tools(bloque: Dict) -> List[Dict]:
    """
    Construye el `tools` schema para la llamada a la API a partir de:
      - las funciones/entidades ya armadas en catalog_cache.get_herramientas()
        (que tiene el schema de cada intención incluyendo tipo, descripción
        y required por entidad), y
      - el enum de modelos del bloque estático (bloque['modelo_enum']).

    Para la entidad 'modelo': reemplaza el tipo 'string' libre por un enum
    cerrado de los valores canónicos. Para 'producto': siempre array de
    strings (sin enum). El resto de entidades pasan sin cambios.

    NOTA — campo 'razonamiento': con tool_choice="required", el modelo no
    emite texto libre en message.content (confirmado empíricamente), por lo
    que el bloque de razonamiento palabra-por-palabra que exige el prompt
    NUNCA se generaba. Se lo inyecta acá como el PRIMER campo de cada
    función (antes de 'modelo' y 'producto') para forzar que el modelo lo
    complete dentro de los argumentos de la tool call, que es el único
    canal de generación de texto disponible bajo tool_choice="required".
    El orden de 'properties' en el dict determina el orden de generación
    de los argumentos, por eso 'razonamiento' va primero.

    Cualquier intención cuyo schema no traiga entidades sigue siendo una
    función válida: se le agrega igualmente 'razonamiento' como único
    campo requerido, para mantener la trazabilidad de la decisión.
    """
    herramientas_base = catalog_cache.get_herramientas()
    modelo_enum = bloque.get('modelo_enum', [])

    razonamiento_schema = {
        'type': 'string',
        'description': (
            'Análisis palabra por palabra del MENSAJE ACTUAL, obligatorio '
            'ANTES de definir producto y modelo. Para cada palabra o grupo '
            'relevante del mensaje, indicá si coincide (exacta, sin tilde, '
            'singular/plural o como alias) con una entrada del DICCIONARIO '
            'DE PRODUCTOS o del DICCIONARIO DE MODELOS, citando el término '
            'canónico exacto encontrado. Si no hay coincidencia, indicalo '
            'también. Formato: "<palabra>" → <canónico> (producto|modelo) '
            'o "<palabra>" → sin match. Cerrá con el resumen de producto '
            '(lista) y modelo (string o null) que vas a usar.'
        ),
    }

    tools = []
    for h in herramientas_base:
        fn = h.get('function', {})
        params = fn.get('parameters', {})
        properties_originales = dict(params.get('properties', {}))
        required = list(params.get('required', []))

        # Inyectar enum en 'modelo' si existe la entidad en esta intención.
        if 'modelo' in properties_originales:
            logger.info(f"[DIAGNOSTICO ENUM MODELO] Total items: {len(modelo_enum)} | Primeros 20: {modelo_enum[:20]}")
            properties_originales['modelo'] = {
                **properties_originales['modelo'],
                'enum': modelo_enum,
            }

        # 'producto' siempre como array de strings
        if 'producto' in properties_originales:
            properties_originales['producto'] = {
                'type': 'array',
                'items': {'type': 'string'},
                'description': properties_originales['producto'].get('description', 'Términos de producto'),
            }

        # 'razonamiento' va primero en el dict para que el modelo lo genere
        # antes que 'modelo'/'producto' (el orden de properties determina
        # el orden de generación de argumentos).
        properties = {
            'razonamiento': razonamiento_schema,
            **properties_originales,
        }

        # Obligatorio en todas las funciones, incluso las que no tienen
        # modelo/producto propios.
        if 'razonamiento' not in required:
            required = ['razonamiento'] + required

        tools.append({
            'type': 'function',
            'function': {
                'name': fn['name'],
                'description': fn.get('description', ''),
                'parameters': {
                    'type': 'object',
                    'properties': properties,
                    'required': required,
                },
            },
        })

    return tools


def _build_system_prompt_static(bloque: Dict) -> str:
    """
    Construye únicamente el bloque estático e inmutable del sistema.
    Al no incluir historial ni mensaje del usuario, se mantiene idéntico
    byte a byte entre peticiones para aprovechar el Automatic Prefix Caching.
    """
    return _PROMPT_TEMPLATE.format(
        intenciones_texto=bloque['intenciones_texto'],
        diccionario_texto=bloque['diccionario_texto'],
        historial_texto="",
        modelo_heredado="",
        mensaje_actual="",
    )


def _build_user_prompt_dynamic(historial_texto: str, modelo_heredado: Optional[str], mensaje_actual: str) -> str:
    """
    Construye la porción dinámica de la petición asignada al rol 'user'.
    """
    return (
        f"HISTORIAL DE LA CONVERSACIÓN:\n{historial_texto or '(sin historial previo)'}\n\n"
        f"MENSAJE ACTUAL DEL USUARIO:\n\"{mensaje_actual}\""
    )


def _parse_tool_call(response) -> Optional[Dict[str, Any]]:
    """
    Extrae la tool call de la respuesta de la API. Devuelve un dict plano.
    """
    choices = response.choices
    if not choices:
        return None

    message = choices[0].message

    # 🔍 DIAGNÓSTICO: capturar todo lo que devolvió el modelo, incluyendo
    # cualquier texto libre (aunque tool_choice="required" no debería
    # dejar espacio para esto, confirmamos empíricamente) y el finish_reason.
    logger.info(
        f"🔬 [DIAGNÓSTICO RAW] finish_reason={choices[0].finish_reason} | "
        f"message.content={message.content!r} | "
        f"tool_calls_count={len(message.tool_calls) if message.tool_calls else 0}"
    )

    if not message.tool_calls:
        logger.warning("⚠️ Clasificador respondió sin tool call (texto libre)")
        return None

    # Nota: Se añade diagnóstico explícito para inspeccionar todos los tool calls
    # devueltos por el modelo antes de seleccionar el elemento del índice 0.
    if len(message.tool_calls) > 1:
        logger.warning(
            f"⚠️ [MÚLTIPLES TOOL CALLS] El modelo devolvió {len(message.tool_calls)} "
            f"tool calls. Se procesará la primera (índice 0)."
        )
        for i, tc in enumerate(message.tool_calls):
            logger.warning(
                f"⚠️ [TOOL CALL #{i}] función={tc.function.name} | args={tc.function.arguments}"
            )

    tool_call = message.tool_calls[0]
    import json
    try:
        args = json.loads(tool_call.function.arguments)
    except Exception as e:
        logger.error(f"❌ Error parseando argumentos de tool call: {e} | raw: {tool_call.function.arguments}")
        return None

    # 🔍 DIAGNÓSTICO: log de argumentos crudos completos, para ver si el
    # modelo devuelve algún campo extra (ej. razonamiento) que no estemos
    # leyendo, y para tener el JSON exacto que decidió el LLM.
    logger.info(f"🔬 [DIAGNÓSTICO ARGS CRUDOS] {tool_call.function.arguments}")

    razonamiento = args.get('razonamiento')
    logger.info(f"🧠 [RAZONAMIENTO LLM] {razonamiento!r}")

    return {
        'intencion': tool_call.function.name,
        'modelo': args.get('modelo') or None,
        'producto': args.get('producto') or [],
        'razonamiento': razonamiento,
        'entidades_extraidas': args,
    }


def clasificar(
    mensaje: str,
    historial_texto: str = "",
    modelo_heredado: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Punto de entrada principal — llamado desde tasks.py por cada mensaje entrante.
    """
    fallback = {
        'intencion': 'sin_clasificar',
        'modelo': modelo_heredado,  # conservar el heredado ante falla técnica
        'producto': [],
        'entidades_extraidas': {},
        'error': None,
    }

    if not mensaje:
        fallback['error'] = 'mensaje vacío'
        return fallback

    try:
        bloque = get_bloque_estatico()
    except Exception as e:
        logger.error(f"❌ Error obteniendo bloque estático: {e}")
        fallback['error'] = f'bloque estático no disponible: {e}'
        return fallback

    try:
        tools = _build_tools(bloque)

        # Nota: Construcción separada de bloques para garantizar compatibilidad con Prefix Caching.
        system_prompt_static = _build_system_prompt_static(bloque)
        user_prompt_dynamic = _build_user_prompt_dynamic(historial_texto, modelo_heredado, mensaje)

        logger.info(
            f"🚀 [PROMPT ENVIADO] Versión Bloque: {bloque.get('version')}\n"
            f"--- SYSTEM PROMPT (ESTÁTICO) ---\n{system_prompt_static}\n"
            f"--- USER PROMPT (DINÁMICO) ---\n{user_prompt_dynamic}"
        )

        client = OpenAI(
            base_url="https://openrouter.ai/api/v1",
            api_key=OPENROUTER_API_KEY,
        )

        response = client.chat.completions.create(
            model="openai/gpt-4o-mini",
            messages=[
                {
                    "role": "system",
                    "content": system_prompt_static,
                    "cache_control": {"type": "ephemeral"}   # ← clave para cachear
                },
                {"role": "user", "content": user_prompt_dynamic}
            ],
            tools=tools,
            tool_choice="required",
            parallel_tool_calls=False,
            temperature=CLASSIFIER_TEMPERATURE,
            max_tokens=CLASSIFIER_MAX_TOKENS,
            extra_body={
                "provider": {"order": ["OpenAI"], "allow_fallbacks": False}
            }
        )

    except Exception as e:
        logger.error(f"❌ Error en llamada al clasificador: {e}")
        fallback['error'] = str(e)
        return fallback

    resultado = _parse_tool_call(response)
    if resultado is None:
        fallback['error'] = 'sin tool call en respuesta'
        return fallback

    # 🔬 DIAGNÓSTICO PRECEDENCIA: distinguir si el modelo resuelto viene de
    # (a) un modelo nuevo detectado en el mensaje actual, (b) el
    # modelo_heredado conservado correctamente porque no hay modelo nuevo,
    # o (c) el modelo_heredado "ganándole" a un posible modelo nuevo por
    # sesgo de frecuencia en el historial — este último es el bug a confirmar.
    modelo_resuelto = resultado['modelo']
    if modelo_heredado and modelo_resuelto and modelo_heredado != modelo_resuelto:
        logger.info(
            f"🔬 [DIAGNÓSTICO PRECEDENCIA] modelo_heredado='{modelo_heredado}' → "
            f"modelo_resuelto='{modelo_resuelto}' | DIFIEREN (mensaje actual definió modelo nuevo)"
        )
    elif modelo_heredado and modelo_resuelto == modelo_heredado:
        menciones_heredado = historial_texto.lower().count(modelo_heredado.lower())
        logger.info(
            f"🔬 [DIAGNÓSTICO PRECEDENCIA] modelo_resuelto == modelo_heredado ('{modelo_heredado}') | "
            f"menciones de ese término en historial_texto={menciones_heredado} | "
            f"mensaje_actual='{mensaje}' | "
            f"⚠️ revisar manualmente si el mensaje_actual contenía un modelo distinto que debió prevalecer"
        )

    logger.info(
        f"🎯 Clasificador | intención={resultado['intencion']} | "
        f"modelo={resultado['modelo']} | producto={resultado['producto']}"
    )

    return {**resultado, 'error': None}