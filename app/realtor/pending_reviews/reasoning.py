import json
import logging
import os
from functools import lru_cache
from pathlib import Path

from app.llm_client import get_openrouter_client

logger = logging.getLogger(__name__)

PROMPT_PATH = Path(__file__).resolve().parents[2] / "contextos" / "realtor" / "prompt_pending_reviews.txt"

TOOLS_PENDING_REVIEWS = [
    {
        "type": "function",
        "function": {
            "name": "generar_tarea_inmediata",
            "description": "El lead requiere atención humana inmediata. Genera una tarea clara para el agente.",
            "parameters": {
                "type": "object",
                "properties": {
                    "razonamiento": {"type": "string","description": "Análisis MUY CONCISO. Máximo 3-4 oraciones (aprox. 10-15 líneas de texto o 300 tokens). Ve directo al grano: qué dijo el cliente y por qué se toma esta decisión."},
                    "temperatura": {"type": "string", "enum": ["Caliente", "Tibio", "Frío", "No interesado"]},
                    "interes": {"type": "string", "enum": ["Casa", "Townhouse", "Inversión", "Rentar", "No determinado"]},
                    "calificacion_lead": {"type": "integer", "minimum": 1, "maximum": 10, "description": "1-10 basado en intención de compra."},
                    "titulo_tarea": {"type": "string", "description": "Título corto y accionable."},
                    "instruccion_para_agente": {"type": "string", "description": "Instrucción detallada. Si incluye un mensaje para enviar, redáctalo aquí aplicando las reglas de oro."}
                },
                "required": ["razonamiento", "temperatura", "interes", "calificacion_lead", "titulo_tarea", "instruccion_para_agente"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "ia_puede_continuar",
            "description": "El mensaje del cliente es simple y el bot puede manejarlo sin intervención humana inmediata.",
            "parameters": {
                "type": "object",
                "properties": {
                    "razonamiento": {"type": "string","description": "Análisis MUY CONCISO. Máximo 3-4 oraciones (aprox. 10-15 líneas de texto o 300 tokens). Ve directo al grano: qué dijo el cliente y por qué se toma esta decisión."},
                    "titulo_tarea": {
                        "type": "string",
                        "description": (
                            "Título corto de una sugerencia de seguimiento OPCIONAL para el agente humano, "
                            "detectada al leer la conversación (ej: '[Sugerencia] Intentar obtener correo', "
                            "'[Sugerencia] Confirmar cita', '[Sugerencia] Reagendar llamada'). "
                            "Usar string vacío '' si no hay ninguna sugerencia relevante en este momento."
                        )
                    }
                },
                "required": ["razonamiento", "titulo_tarea"]
            }
        }
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
                    "razonamiento": {"type": "string","description": "Análisis MUY CONCISO. Máximo 3-4 oraciones (aprox. 10-15 líneas de texto o 300 tokens). Ve directo al grano: qué dijo el cliente y por qué se toma esta decisión."},
                },
                "required": ["motivo", "razonamiento"]
            }
        }
    }
]


@lru_cache(maxsize=1)
def _get_static_prompt() -> str:
    return PROMPT_PATH.read_text(encoding="utf-8")


def analyze_pending_review(contact_name: str, historial_texto: str) -> dict:
    """
    Corre el razonamiento del LLM sobre el historial de un lead con actividad reciente.
    Incluye diagnóstico de JSON para depurar errores de formato del LLM.
    """
    prompt = _get_static_prompt().format(
        contact_name=contact_name,
        historial_texto=historial_texto,
    )

    client = get_openrouter_client()
    logger.info(f"🧠 Analizando pending review para '{contact_name}'")

    model_name = os.environ.get("PENDING_REVIEWS_MODEL") or os.environ.get("COOL_LEADS_MODEL", "anthropic/claude-sonnet-5")
    temperature = float(os.environ.get("PENDING_REVIEWS_TEMPERATURE") or os.environ.get("COOL_LEADS_TEMPERATURE", "0.3"))
    max_tokens = int(os.environ.get("PENDING_REVIEWS_MAX_TOKENS") or os.environ.get("COOL_LEADS_MAX_TOKENS", "800"))

    response = client.chat.completions.create(
        model=model_name,
        temperature=temperature,
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": prompt}],
        tools=TOOLS_PENDING_REVIEWS,
        tool_choice="required",
    )

    message = response.choices[0].message
    if not message.tool_calls:
        raise RuntimeError(
            f"El modelo {model_name} ignoró tool_choice='required' y devolvió texto plano. "
            f"Contenido: {message.content}"
        )

    tool_call = message.tool_calls[0]
    raw_arguments = tool_call.function.arguments
    
    # 👉 DIAGNÓSTICO DE JSON: Si el LLM genera JSON inválido, lo registramos para ver el error exacto.
    try:
        args = json.loads(raw_arguments)
    except json.JSONDecodeError as e:
        logger.error(f"❌ Error decodificando JSON del LLM: {e}")
        logger.error(f"📄 Argumentos crudos recibidos del LLM:\n{raw_arguments}")
        raise RuntimeError(f"El modelo devolvió JSON inválido en los argumentos de la herramienta. Error: {e}")

    accion = tool_call.function.name
    logger.info(f"✅ Decisión: {accion} | {contact_name}")

    return {"accion": accion, **args}