"""Tool y handler: lead pidió STOP / no contacto / es spam. No se crea tarea."""
import logging

import httpx

from app.realtor.shared.schemas import (
    RAZONAMIENTO_SCHEMA,
    TEMPERATURA_SCHEMA,
    NOTA_BITACORA_SCHEMA,
    HITO_RESUELTO_SCHEMA,
)
from app.realtor.shared.context import ExecutionContext

logger = logging.getLogger(__name__)

NOMBRE_TOOL = "marcar_sin_accion"


def get_schema(tareas_pendientes_ghl: list[dict]) -> dict:
    return {
        "type": "function",
        "function": {
            "name": NOMBRE_TOOL,
            "description": "El lead dijo STOP, no le llamen, o es spam. No se debe crear tarea ni contactar.",
            "parameters": {
                "type": "object",
                "properties": {
                    "motivo": {"type": "string", "description": "Motivo del descarte."},
                    "razonamiento": RAZONAMIENTO_SCHEMA,
                    "temperatura": TEMPERATURA_SCHEMA,
                    "nota_bitacora": NOTA_BITACORA_SCHEMA,
                    "hito_resuelto": HITO_RESUELTO_SCHEMA,
                },
                "required": ["motivo", "razonamiento", "temperatura", "nota_bitacora"],
            },
        },
    }


PROMPT_FRAGMENT = """
OPCIÓN C: `marcar_sin_accion`
Úsala SOLO si el lead dijo explícitamente "STOP", "no me llamen", "ya compré y no quiero referir", o es spam evidente.
- Asigna siempre `temperatura` = "No interesado" en este caso.
"""


def execute_handler(result: dict, ctx: ExecutionContext) -> dict:
    webhook_payload = {
        "task_id": ctx.task_id,
        "contact_id": ctx.contact_id,
        "contact_name": ctx.contact_name,
        "accion": "descartar",
        "temperatura": "No interesado",
        "task_title": "",
        "motivo": str(result.get("motivo", "")),
        "razonamiento": str(result.get("razonamiento", "")),
        "nota_bitacora": ctx.nota_bitacora,
        "custom_field_value": ctx.fecha_hora_revision,
    }

    logger.info(f"🚀 Disparando webhook | accion={NOMBRE_TOOL} | {ctx.contact_name}")
    response = httpx.post(ctx.webhook_url, json=webhook_payload, timeout=15.0)
    response.raise_for_status()
    resp_json = response.json()
    trigger_id = resp_json.get("triggerId") or resp_json.get("id") or "N/A"
    logger.info(f"✅ Webhook OK | {ctx.contact_name} | trigger_id={trigger_id}")

    return {"status": "success", "trigger_id": trigger_id}