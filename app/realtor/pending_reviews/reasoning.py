import json
import logging
import os
from functools import lru_cache
from pathlib import Path

from app.llm_client import get_openrouter_client

logger = logging.getLogger(__name__)

PROMPT_PATH = Path(__file__).resolve().parents[2] / "contextos" / "realtor" / "prompt_pending_reviews.txt"

NOTA_BITACORA_SCHEMA = {
    "type": "string",
    "description": (
        "Cuerpo de la bitácora para el historial del contacto en GHL: un resumen "
        "cronológico de TODA la conversación hasta ahora (no solo del último "
        "mensaje), regenerado completo en cada análisis. Un renglón por hito "
        "relevante (inicio de conversación, envío de información, falta de "
        "respuesta, pregunta de precio, etc.), con el formato exacto "
        "'DD MES_ABREV_MAYUS YYYY <resumen breve del hito>' (ej: "
        "'25 JUL 2026 Jaime inicia conversación preguntando por el proyecto de "
        "casas en Homestead, el agente respondió enviando la información'). Las "
        "fechas deben salir de los timestamps reales presentes en el historial, "
        "NUNCA inventadas. NO incluyas encabezado ni la fecha de hoy (eso se "
        "agrega automáticamente en el sistema). NO repitas el 'razonamiento' "
        "palabra por palabra ni uses términos internos del sistema (temperatura, "
        "calificación, nombres de herramientas, etc.)."
    ),
}

TOOLS_PENDING_REVIEWS = [
    {
        "type": "function",
        "function": {
            "name": "generar_tarea_inmediata",
            "description": "El lead requiere atención humana inmediata. Genera una tarea clara para el agente.",
            "parameters": {
                "type": "object",
                "properties": {
                    "razonamiento": {"type": "string", "description": "Análisis MUY CONCISO. Máximo 3-4 oraciones (aprox. 10-15 líneas de texto o 300 tokens). Ve directo al grano: qué dijo el cliente y por qué se toma esta decisión."},
                    "temperatura": {"type": "string", "enum": ["Caliente", "Tibio", "Frío", "No interesado"]},
                    "interes": {"type": "string", "enum": ["Casa", "Townhouse", "Inversión", "Rentar", "No determinado"]},
                    "calificacion_lead": {"type": "integer", "minimum": 1, "maximum": 10, "description": "1-10 basado en intención de compra."},
                    "titulo_tarea": {"type": "string", "description": "Título corto y accionable."},
                    "instruccion_para_agente": {"type": "string", "description": "Instrucción detallada. Si incluye un mensaje para enviar, redáctalo aquí aplicando las reglas de oro."},
                    "nota_bitacora": NOTA_BITACORA_SCHEMA,
                },
                "required": [
                    "razonamiento", "temperatura", "interes",
                    "calificacion_lead", "titulo_tarea",
                    "instruccion_para_agente", "nota_bitacora",
                ],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "ia_puede_continuar",
            "description": "El mensaje del cliente es simple y el bot puede manejarlo sin intervención humana inmediata.",
            "parameters": {
                "type": "object",
                "properties": {
                    "razonamiento": {"type": "string", "description": "Análisis MUY CONCISO. Máximo 3-4 oraciones (aprox. 10-15 líneas de texto o 300 tokens). Ve directo al grano: qué dijo el cliente y por qué se toma esta decisión."},
                    "titulo_tarea": {
                        "type": "string",
                        "description": (
                            "Título corto de una sugerencia de seguimiento OPCIONAL para el agente humano, "
                            "detectada al leer la conversación (ej: '[Sugerencia] Intentar obtener correo', "
                            "'[Sugerencia] Confirmar cita', '[Sugerencia] Reagendar llamada'). "
                            "Usar string vacío '' si no hay ninguna sugerencia relevante en este momento."
                        ),
                    },
                    "nota_bitacora": NOTA_BITACORA_SCHEMA,
                },
                "required": ["razonamiento", "titulo_tarea", "nota_bitacora"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "marcar_sin_accion",
            "description": "El lead dijo STOP, no le llamen, o es spam. No se debe crear tarea ni contactar.",
            "parameters": {
                "type": "object",
                "properties": {
                    "motivo": {"type": "string", "description": "Motivo del descarte."},
                    "razonamiento": {"type": "string", "description": "Análisis MUY CONCISO. Máximo 3-4 oraciones (aprox. 10-15 líneas de texto o 300 tokens). Ve directo al grano: qué dijo el cliente y por qué se toma esta decisión."},
                    "nota_bitacora": NOTA_BITACORA_SCHEMA,
                },
                "required": ["motivo", "razonamiento", "nota_bitacora"],
            },
        },
    },
]


@lru_cache(maxsize=1)
def _get_static_prompt() -> str:
    return PROMPT_PATH.read_text(encoding="utf-8")


def analyze_pending_review(contact_name: str, historial_texto: str, max_intentos: int = 2) -> dict:
    """
    Corre el razonamiento del LLM sobre el historial de un lead con actividad reciente.
    Si el LLM devuelve JSON inválido en los argumentos de la tool, reintenta hasta
    `max_intentos` veces. Distingue dos causas posibles:
      1. Truncamiento por max_tokens (finish_reason == "length"): la respuesta se
         cortó a mitad de camino. Se reintenta con más tokens.
      2. JSON mal formado por otra razón (ej. comillas sin escapar): se reintenta
         con un recordatorio explícito del error.
    """
    prompt_base = _get_static_prompt().format(
        contact_name=contact_name,
        historial_texto=historial_texto,
    )

    client = get_openrouter_client()
    logger.info(f"🧠 Analizando pending review para '{contact_name}'")

    model_name = os.environ.get("PENDING_REVIEWS_MODEL") or os.environ.get("COOL_LEADS_MODEL", "anthropic/claude-sonnet-5")
    temperature = float(os.environ.get("PENDING_REVIEWS_TEMPERATURE") or os.environ.get("COOL_LEADS_TEMPERATURE", "0.3"))
    # 👉 Default subido de 800 a 1800: nota_bitacora ahora exige un resumen
    # cronológico de TODA la conversación, y con leads activos (varias
    # interacciones) el total de campos supera fácil los 800 tokens antiguos.
    max_tokens = int(os.environ.get("PENDING_REVIEWS_MAX_TOKENS") or os.environ.get("COOL_LEADS_MAX_TOKENS", "1800"))

    prompt_actual = prompt_base
    tokens_actuales = max_tokens
    ultimo_error: json.JSONDecodeError | None = None

    for intento in range(1, max_intentos + 1):
        response = client.chat.completions.create(
            model=model_name,
            temperature=temperature,
            max_tokens=tokens_actuales,
            messages=[{"role": "user", "content": prompt_actual}],
            tools=TOOLS_PENDING_REVIEWS,
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
            logger.error(f"📄 Argumentos crudos recibidos del LLM:\n{raw_arguments}")

            if intento < max_intentos:
                if fue_truncado:
                    # 👉 Truncamiento real: subir el presupuesto de tokens y pedir
                    # explícitamente que sea más conciso en nota_bitacora.
                    tokens_actuales = int(tokens_actuales * 1.5)
                    prompt_actual = (
                        f"{prompt_base}\n\n"
                        f"ADVERTENCIA: tu respuesta anterior se cortó antes de completar el JSON "
                        f"(te quedaste sin espacio). En 'nota_bitacora', si hay más de 8 hitos "
                        f"relevantes, resume los más antiguos en un solo renglón agregado y detalla "
                        f"solo los últimos 6-8 hitos más recientes. Sé más conciso en general y "
                        f"asegúrate de cerrar completamente el JSON con todos los campos requeridos."
                    )
                    logger.warning(
                        f"⚠️ Truncamiento detectado, reintentando con max_tokens={tokens_actuales} "
                        f"| {contact_name}"
                    )
                else:
                    prompt_actual = (
                        f"{prompt_base}\n\n"
                        f"ADVERTENCIA: tu respuesta anterior tenía JSON inválido y no pudo "
                        f"procesarse (error: {e}). La causa más común es una comilla doble (\") "
                        f"sin escapar dentro de un valor de texto. Genera de nuevo la respuesta "
                        f"completa, revisando que NINGÚN campo de texto contenga comillas dobles "
                        f"sin escapar."
                    )
                continue
            raise RuntimeError(
                f"El modelo devolvió JSON inválido en los argumentos de la herramienta "
                f"tras {max_intentos} intentos (finish_reason={finish_reason}). Error: {e}"
            )

        accion = tool_call.function.name
        logger.info(f"✅ Decisión: {accion} | {contact_name} | intento {intento}/{max_intentos}")

        return {"accion": accion, **args}

    # Inalcanzable en la práctica: el loop siempre retorna o lanza excepción.
    raise RuntimeError(f"Fallo inesperado analizando a {contact_name}. Último error: {ultimo_error}")