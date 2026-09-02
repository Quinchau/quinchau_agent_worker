import json
import logging
import os
from functools import lru_cache
from pathlib import Path

from app.llm_client import get_openrouter_client

logger = logging.getLogger(__name__)

PROMPT_PATH = Path(__file__).resolve().parents[2] / "contextos" / "realtor" / "prompt_cool_leads.txt"

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "crear_tarea",
            "description": (
                "El vendedor debe tomar una acción sobre este lead: reactivarlo o marcarlo "
                "como Lost. SIEMPRE se crea una tarea visible para el vendedor."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "razonamiento": {"type": "string", "description": "Análisis paso a paso antes de decidir."},
                    "nivel_interes": {"type": "string", "enum": ["alto", "medio", "bajo"]},
                    "tipo_tarea": {
                        "type": "string",
                        "enum": ["reactivacion", "marcar_lost"],
                        "description": (
                            "'reactivacion' si el lead vale la pena retomar. "
                            "'marcar_lost' si corresponde sugerir al vendedor pasar el lead a Lost "
                            "(por interés bajo o por regla de umbral de inactividad)."
                        ),
                    },
                    "task_title": {"type": "string"},
                    "task_body": {"type": "string"},
                    "prioridad": {"type": "string", "enum": ["alta", "media", "baja"]},
                    "dias_para_seguimiento": {"type": "integer"},
                },
                "required": [
                    "razonamiento", "nivel_interes", "tipo_tarea",
                    "task_title", "task_body", "prioridad", "dias_para_seguimiento",
                ],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "descartar",
            "description": (
                "El vendedor NO debe tomar ninguna acción. No se crea tarea. "
                "Usar SOLO para casos de interés nulo real: spam, número equivocado, "
                "el lead ya cerró por otro medio, o pidió explícitamente no ser contactado."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "razonamiento": {"type": "string", "description": "Análisis paso a paso antes de decidir."},
                    "nivel_interes": {"type": "string", "enum": ["nulo"]},
                    "motivo": {"type": "string", "description": "Motivo breve del descarte, para dejar registro."},
                },
                "required": ["razonamiento", "nivel_interes", "motivo"],
            },
        },
    },
]


@lru_cache(maxsize=1)
def _get_static_prompt() -> str:
    return PROMPT_PATH.read_text(encoding="utf-8")


def analyze_lead(contact_name: str, days_inactive: int, total_messages: int, historial_texto: str) -> dict:
    """
    Corre el razonamiento del LLM sobre el historial de un lead.
    NO atrapa excepciones (Modo B, §7.7): si el LLM falla, el job de RQ debe fallar visiblemente.
    """
    prompt = _get_static_prompt().format(
        contact_name=contact_name,
        days_inactive=days_inactive,
        total_messages=total_messages,
        historial_texto=historial_texto,
    )

    client = get_openrouter_client()
    logger.info(f"🧠 Analizando lead '{contact_name}' ({days_inactive} días inactivo)")

    response = client.chat.completions.create(
        model=os.environ["COOL_LEADS_MODEL"],
        temperature=float(os.environ.get("COOL_LEADS_TEMPERATURE", "0.3")),
        max_tokens=int(os.environ.get("COOL_LEADS_MAX_TOKENS", "700")),
        messages=[{"role": "user", "content": prompt}],
        tools=TOOLS,
        tool_choice="required",
    )

    message = response.choices[0].message
    if not message.tool_calls:
        raise RuntimeError(
            f"El modelo {os.environ['COOL_LEADS_MODEL']} ignoró tool_choice='required' "
            f"y devolvió texto plano en lugar de una función. Contenido: {message.content}"
        )

    tool_call = message.tool_calls[0]
    args = json.loads(tool_call.function.arguments)

    accion = "crear_tarea" if tool_call.function.name == "crear_tarea" else "descartar"

    logger.info(
        f"✅ Decisión: {accion} | tipo_tarea={args.get('tipo_tarea', 'n/a')} | "
        f"interés={args.get('nivel_interes')} | {contact_name}"
    )

    return {"accion": accion, **args}