"""Tool y handler: el bot puede seguir la conversación sin intervención humana inmediata."""
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

NOMBRE_TOOL = "ia_puede_continuar"


def get_schema() -> dict:
    return {
        "type": "function",
        "function": {
            "name": NOMBRE_TOOL,
            "description": "El mensaje del cliente es simple y el bot puede manejarlo sin intervención humana inmediata.",
            "parameters": {
                "type": "object",
                "properties": {
                    "razonamiento": RAZONAMIENTO_SCHEMA,
                    "temperatura": TEMPERATURA_SCHEMA,
                    "titulo_tarea": {
                        "type": "string",
                        "description": "Título corto de una sugerencia de seguimiento OPCIONAL (ej: '[Sugerencia] Intentar obtener correo'). String vacío '' si no aplica.",
                    },
                    "nota_bitacora": NOTA_BITACORA_SCHEMA,
                    "hito_resuelto": HITO_RESUELTO_SCHEMA,
                },
                "required": ["razonamiento", "temperatura", "titulo_tarea", "nota_bitacora"],
            },
        },
    }


PROMPT_FRAGMENT = """
OPCIÓN B: `ia_puede_continuar`
Úsala si el cliente solo saludó, confirmó un dato simple (ej: "sí, gracias") o hizo una pregunta que el bot ya puede responder automáticamente sin intervención humana.
- Debes asignar `temperatura` (Caliente / Tibio / Frio / No interesado) igual que en la OPCIÓN A, según la intención de compra mostrada hasta el momento.
- Aunque no se requiera intervención humana ahora, revisa si hay una oportunidad de seguimiento relevante (correo aún no capturado, cita por confirmar, llamada pendiente, recontactar en unos días, etc.) y complétala en `titulo_tarea` con formato "[Sugerencia] <acción concreta>", o deja `titulo_tarea` vacío si no aplica ninguna.
- En `razonamiento`, además de explicar por qué no se necesita intervención inmediata, justifica brevemente la temperatura asignada y, si generaste una sugerencia en `titulo_tarea`, explica por qué esa es la sugerencia pertinente en este momento.
"""


def execute_handler(result: dict, ctx: ExecutionContext) -> dict:
    webhook_payload = {
        "task_id": ctx.task_id,
        "contact_id": ctx.contact_id,
        "contact_name": ctx.contact_name,
        "accion": "ia_continua",
        "temperatura": result.get("temperatura", "No determinado"),
        "task_title": str(result.get("titulo_tarea", "")),
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