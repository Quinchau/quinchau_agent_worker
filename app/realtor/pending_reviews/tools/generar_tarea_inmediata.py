"""Tool y handler: crear una tarea NUEVA en GHL (vía webhook)."""
import logging

import httpx

from app.realtor.shared.schemas import (
    RAZONAMIENTO_SCHEMA,
    TEMPERATURA_SCHEMA,
    NOTA_BITACORA_SCHEMA,
    HITO_RESUELTO_SCHEMA,
    DIAS_OFFSET_SCHEMA,
    HORAS_OFFSET_SCHEMA,
)
from app.realtor.shared.scheduling import aplicar_piso_temperatura, formatear_due_date_ghl_webhook
from app.realtor.shared.context import ExecutionContext

logger = logging.getLogger(__name__)

NOMBRE_TOOL = "generar_tarea_inmediata"


def get_schema(tareas_pendientes_ghl: list[dict]) -> dict:
    return {
        "type": "function",
        "function": {
            "name": NOMBRE_TOOL,
            "description": "El lead requiere atención humana inmediata. Genera una tarea NUEVA.",
            "parameters": {
                "type": "object",
                "properties": {
                    "razonamiento": RAZONAMIENTO_SCHEMA,
                    "temperatura": TEMPERATURA_SCHEMA,
                    "interes": {"type": "string", "enum": ["Casa", "Townhouse", "Inversión", "Rentar", "No determinado"]},
                    "calificacion_lead": {"type": "integer", "minimum": 1, "maximum": 10},
                    "titulo_tarea": {"type": "string", "description": "Título corto y accionable (SIN fechas absolutas)."},
                    "instruccion_para_agente": {"type": "string", "description": "Instrucción detallada. Si incluye un mensaje para enviar, redáctalo aquí aplicando las reglas de oro."},
                    "dias_para_vencer": DIAS_OFFSET_SCHEMA,
                    "horas_para_vencer": HORAS_OFFSET_SCHEMA,
                    "nota_bitacora": NOTA_BITACORA_SCHEMA,
                    "hito_resuelto": HITO_RESUELTO_SCHEMA,
                },
                "required": [
                    "razonamiento", "temperatura", "interes", "calificacion_lead",
                    "titulo_tarea", "instruccion_para_agente", "dias_para_vencer",
                    "horas_para_vencer", "nota_bitacora",
                ],
            },
        },
    }


PROMPT_FRAGMENT = """
OPCIÓN A: `generar_tarea_inmediata`
Úsala si el lead requiere atención humana (pregunta compleja, pidió precio, mostró interés, o hay que aplicar un seguimiento de los 14 escenarios) Y no existe ya una tarea abierta con el mismo objetivo (si existe, usa OPCIÓN D en su lugar).
- Debes proporcionar un `titulo_tarea` claro y corto (ver REGLA DE TÍTULOS DE TAREA).
- Debes proporcionar `instruccion_para_agente`: Instrucciones precisas de qué hacer. Si requiere enviar un mensaje, incluye el borrador exacto del mensaje aplicando las REGLAS DE ORO (corto, humano, pidiendo correo, sin dirección exacta).
- Debes indicar `dias_para_vencer` y `horas_para_vencer` según la REGLA DE FECHA Y HORA DE LA TAREA y la REGLA DE PLAZO MÍNIMO SEGÚN TEMPERATURA.
- Debes asignar `temperatura` (Caliente / Tibio / Frio / No interesado) según la intención de compra mostrada en la última interacción relevante.
"""


def execute_handler(result: dict, ctx: ExecutionContext) -> dict:
    """Construye el webhook_payload para crear una tarea nueva y lo dispara."""
    dias_para_vencer = aplicar_piso_temperatura(
        int(result.get("dias_para_vencer", 0)),
        result.get("temperatura", "Frio"),
        ctx.contact_name,
    )

    try:
        nueva_fecha_limite = formatear_due_date_ghl_webhook(
            dias_para_vencer=dias_para_vencer,
            horas_para_vencer=int(result.get("horas_para_vencer", 0)),
            ahora_miami=ctx.ahora_miami,
        )
    except (TypeError, ValueError) as e:
        logger.error(f"❌ Error calculando fecha para webhook | {ctx.contact_name}: {e}")
        nueva_fecha_limite = ""

    webhook_payload = {
        "task_id": ctx.task_id,
        "contact_id": ctx.contact_id,
        "contact_name": ctx.contact_name,
        "accion": "crear_tarea",
        "temperatura": result.get("temperatura", "No determinado"),
        "interes": result.get("interes", "No determinado"),
        "calificacion_lead": int(result.get("calificacion_lead", 5)),
        "task_title": str(result.get("titulo_tarea", "")),
        "task_body": str(result.get("instruccion_para_agente", "")),
        "razonamiento": str(result.get("razonamiento", "")),
        "nota_bitacora": ctx.nota_bitacora,
        "custom_field_value": ctx.fecha_hora_revision,
        "nueva_fecha_limite": nueva_fecha_limite,
    }

    logger.info(f"🚀 Disparando webhook | accion={NOMBRE_TOOL} | {ctx.contact_name}")
    response = httpx.post(ctx.webhook_url, json=webhook_payload, timeout=15.0)
    response.raise_for_status()
    resp_json = response.json()
    trigger_id = resp_json.get("triggerId") or resp_json.get("id") or "N/A"
    logger.info(f"✅ Webhook OK | {ctx.contact_name} | trigger_id={trigger_id}")

    return {"status": "success", "trigger_id": trigger_id}