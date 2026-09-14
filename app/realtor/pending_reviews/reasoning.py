import json
import logging
import os
import re
from datetime import datetime
from functools import lru_cache
from pathlib import Path

from app.llm_client import get_openrouter_client
from app.realtor.shared.scheduling import MIAMI_TZ, formatear_fecha_hoy
from app.realtor.pending_reviews.tools import (
    generar_tarea_inmediata,
    ia_puede_continuar,
    marcar_sin_accion,
    actualizar_tarea_existente,
)

logger = logging.getLogger(__name__)

PROMPT_STATIC_PATH = Path(__file__).resolve().parents[2] / "contextos" / "realtor" / "prompt_pending_reviews_static.txt"

MARCADOR_OPCIONES = "<<OPCIONES_DE_ACCION>>"

# Orden en que se presentan las opciones al modelo (A, B, C, D). El texto de
# cada una vive junto a su tool, en PROMPT_FRAGMENT — reasoning.py solo las
# agrega, no las redefine.
TOOL_MODULES = [
    generar_tarea_inmediata,
    ia_puede_continuar,
    marcar_sin_accion,
    actualizar_tarea_existente,
]


def _formatear_tareas_pendientes(tareas: list[dict]) -> str:
    """Convierte el array de tareas pendientes de GHL en un bloque de texto legible."""
    if not tareas:
        return "No hay tareas pendientes abiertas actualmente para este contacto."

    lineas = []
    for t in tareas:
        titulo = t.get("titulo", "(sin título)")
        descripcion_html = t.get("descripcion", "") or ""
        descripcion_limpia = re.sub(r"<[^>]+>", "", descripcion_html).strip()
        fecha_limite = t.get("fecha_limite", "sin fecha")
        task_id = t.get("id", "sin-id")
        lineas.append(f'- ID "{task_id}" | "{titulo}" (vence {fecha_limite}): {descripcion_limpia}')

    return "\n".join(lineas)


@lru_cache(maxsize=1)
def _get_static_prompt() -> str:
    """
    Carga la plantilla estática una sola vez y la arma insertando el
    PROMPT_FRAGMENT de cada tool en el marcador <<OPCIONES_DE_ACCION>>.

    El resultado es 100% estático e idéntico en TODAS las llamadas —
    incluye las 4 opciones sin importar si el contacto tiene tareas
    pendientes o no — para maximizar el hit rate del prompt caching
    (cache_control ephemeral). Si se condicionara la inclusión de la
    OPCIÓN D según haya o no tareas abiertas, se perdería cache en cada
    contacto sin tareas pendientes.
    """
    plantilla = PROMPT_STATIC_PATH.read_text(encoding="utf-8")
    fragmentos = "\n".join(modulo.PROMPT_FRAGMENT for modulo in TOOL_MODULES)
    return plantilla.replace(MARCADOR_OPCIONES, fragmentos)


def _build_tools() -> list[dict]:
    """
    Arma la lista de tools para esta llamada. Las 4 tools se incluyen
    SIEMPRE, sin condicionar 'actualizar_tarea_existente' a que existan
    tareas abiertas: su schema ya no lleva un enum de IDs dinámico (esa
    validación se movió a execute_handler), así que el array completo de
    tools queda idéntico byte a byte en TODAS las llamadas.

    Esto es necesario porque en el orden de serialización de Anthropic
    (tools -> system -> messages), 'tools' precede al bloque_estatico
    marcado con cache_control: si tools cambiara de una llamada a otra,
    invalidaría el cache aunque el texto del prompt fuera idéntico.
    """
    return [
        generar_tarea_inmediata.get_schema(),
        ia_puede_continuar.get_schema(),
        marcar_sin_accion.get_schema(),
        actualizar_tarea_existente.get_schema(),
    ]


def analyze_pending_review(
    contact_name: str,
    historial_texto: str,
    tareas_pendientes_ghl: list[dict] | None = None,
    señal_cierre: str = "seguir",  # <-- NUEVO PARÁMETRO
    max_intentos: int = 2,
) -> dict:
    
    tareas_pendientes_ghl = tareas_pendientes_ghl or []

    ahora_miami = datetime.now(MIAMI_TZ)
    fecha_hoy_str = formatear_fecha_hoy(ahora_miami)
    hora_actual_str = ahora_miami.strftime("%H:%M")

    # BLOQUE A: Estático, cacheable (reglas, formatos, opciones de las tools)
    bloque_estatico = _get_static_prompt()

    bloque_variable = (
        f"--- CONTEXTO DE LA EJECUCIÓN ---\n"
        f"HOY ES: {fecha_hoy_str}\n"
        f"HORA ACTUAL (Miami): {hora_actual_str}\n\n"
        f"SEÑAL DE CIERRE DE VENTANA: {señal_cierre.upper()}\n"
        f"(Instrucción: Si es 'EVALUAR', determina si el hito está resuelto. Si es 'SEGUIR' o 'CERRAR', no evalúes el hito).\n\n"
        f"TAREAS PENDIENTES YA ABIERTAS PARA ESTE CONTACTO EN GHL:\n"
        f"{_formatear_tareas_pendientes(tareas_pendientes_ghl)}\n\n"
        f"Contacto: {contact_name}\n\n"
        f"HISTORIAL COMPLETO DE CONVERSACIÓN (Incluye bitácora fija + ventana de mensajes nuevos):\n"
        f"{historial_texto}"
    )

    tools = _build_tools()
    client = get_openrouter_client()

    model_name = os.environ.get("PENDING_REVIEWS_MODEL") or os.environ.get("COOL_LEADS_MODEL", "anthropic/claude-sonnet-5")
    temperature = float(os.environ.get("PENDING_REVIEWS_TEMPERATURE") or os.environ.get("COOL_LEADS_TEMPERATURE", "0.3"))
    max_tokens = int(os.environ.get("PENDING_REVIEWS_MAX_TOKENS") or os.environ.get("COOL_LEADS_MAX_TOKENS", "1800"))

    # Estructura de mensaje con Prompt Caching (Fase 0)
    messages = [{
        "role": "user",
        "content": [
            {
                "type": "text",
                "text": bloque_estatico,
                "cache_control": {"type": "ephemeral", "ttl": "1h"},
            },
            {
                "type": "text",
                "text": bloque_variable,
            }
        ]
    }]

    tokens_actuales = max_tokens
    ultimo_error: json.JSONDecodeError | None = None

    for intento in range(1, max_intentos + 1):
        response = client.chat.completions.create(
            model=model_name,
            temperature=temperature,
            max_tokens=tokens_actuales,
            messages=messages,
            tools=tools,
            tool_choice="required",
        )

        choice = response.choices[0]
        message = choice.message

        if not message.tool_calls:
            raise RuntimeError(
                f"El modelo {model_name} ignoró tool_choice='required' y devolvió texto plano. "
                f"Contenido: {message.content}"
            )

        tool_call = message.tool_calls[0]
        raw_arguments = tool_call.function.arguments
        finish_reason = getattr(choice, "finish_reason", None)
        fue_truncado = finish_reason == "length"

        try:
            args = json.loads(raw_arguments)
        except json.JSONDecodeError as e:
            ultimo_error = e
            logger.error(
                f"❌ Error decodificando JSON del LLM (intento {intento}/{max_intentos}) "
                f"| {contact_name} | finish_reason={finish_reason}: {e}"
            )
            logger.debug(f"📄 Argumentos crudos recibidos:\n{raw_arguments}")

            if intento < max_intentos:
                # Modificamos SOLO el bloque variable para el reintento.
                # Esto preserva el caché del bloque estático (ephemeral).
                if fue_truncado:
                    tokens_actuales = int(tokens_actuales * 1.5)
                    messages[0]["content"][1]["text"] = (
                        f"{bloque_variable}\n\n"
                        f"⚠️ ADVERTENCIA: tu respuesta anterior se cortó antes de completar el JSON "
                        f"(te quedaste sin espacio). En 'nota_bitacora', resume los hitos más antiguos "
                        f"en un solo renglón y detalla solo los últimos 6-8. Sé conciso y cierra el JSON."
                    )
                    logger.warning(f"⚠️ Truncamiento detectado, reintentando con max_tokens={tokens_actuales} | {contact_name}")
                else:
                    messages[0]["content"][1]["text"] = (
                        f"{bloque_variable}\n\n"
                        f"⚠️ ADVERTENCIA: tu respuesta anterior tenía JSON inválido (error: {e}). "
                        f"La causa más común es una comilla doble (\") sin escapar. Genera de nuevo "
                        f"la respuesta completa, revisando que NINGÚN campo de texto contenga comillas dobles sin escapar."
                    )
                continue

            raise RuntimeError(
                f"El modelo devolvió JSON inválido en los argumentos de la herramienta "
                f"tras {max_intentos} intentos (finish_reason={finish_reason}). Error: {e}"
            )

        accion = tool_call.function.name
        logger.info(f"✅ Decisión: {accion} | {contact_name} | intento {intento}/{max_intentos}")

        # Monitoreo de efectividad del caché
        usage = getattr(response, "usage", None)
        if usage:
            cache_read = getattr(usage, "cache_read_input_tokens", 0)
            if not cache_read:
                prompt_details = getattr(usage, "prompt_tokens_details", None)
                if prompt_details is not None:
                    cache_read = getattr(prompt_details, "cached_tokens", 0) or 0

            if cache_read > 0:
                logger.info(f"💾 Cache HIT: {cache_read} tokens leídos de caché para {contact_name}")
            else:
                logger.debug(f"📝 Cache MISS (primera vez o TTL expirado) para {contact_name}")

        return {"accion": accion, **args}

    raise RuntimeError(f"Fallo inesperado analizando a {contact_name}. Último error: {ultimo_error}")