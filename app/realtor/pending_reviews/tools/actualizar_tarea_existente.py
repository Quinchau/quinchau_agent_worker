"""Tool y handler: actualizar una tarea existente en GHL."""
import logging
from datetime import datetime

import httpx

from app.realtor.shared.schemas import (
    RAZONAMIENTO_SCHEMA,
    TEMPERATURA_SCHEMA,
    NOTA_BITACORA_SCHEMA,
    HITO_RESUELTO_SCHEMA,
    DIAS_OFFSET_SCHEMA,
    HORAS_OFFSET_SCHEMA,
)
from app.realtor.shared.scheduling import MIAMI_TZ, aplicar_piso_temperatura, calcular_due_date
from app.realtor.shared.context import ExecutionContext

logger = logging.getLogger(__name__)

NOMBRE_TOOL = "actualizar_tarea_existente"
GHL_BASE_URL = "https://services.leadconnectorhq.com"


def get_schema() -> dict:
    """
    Schema 100% estático — no depende de tareas_pendientes_ghl ni de
    ningún otro dato del contacto. La validación del ID de la tarea a
    actualizar contra las tareas reales del contacto se hace en
    execute_handler(), no acá, para que este schema (y por lo tanto el
    array completo de `tools`, que precede al bloque_estatico cacheado)
    sea idéntico en todas las llamadas.
    """
    return {
        "type": "function",
        "function": {
            "name": NOMBRE_TOOL,
            "description": (
                "Úsala SOLO si la nueva solicitud tiene el EXACTO MISMO PROPÓSITO "
                "que una tarea ya abierta. Si el motivo o la acción son distintos, "
                "usa 'generar_tarea_inmediata'."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "razonamiento": RAZONAMIENTO_SCHEMA,
                    "temperatura": TEMPERATURA_SCHEMA,
                    "id_tarea_a_actualizar": {
                        "type": "string",
                        "description": (
                            "ID exacto de la tarea a actualizar, tomado LITERALMENTE de la "
                            "lista de TAREAS PENDIENTES YA ABIERTAS en el contexto de este "
                            "contacto. NUNCA inventes uno ni lo tomes de otro contacto."
                        ),
                    },
                    "nuevo_titulo": {
                        "type": "string",
                        "description": "Nuevo título de la tarea, o string vacío '' si el actual sigue siendo válido.",
                    },
                    "nueva_instruccion": {
                        "type": "string",
                        "description": "Nueva instrucción para el agente, o string vacío '' si la actual sigue siendo válida.",
                    },
                    "reagendar": {
                        "type": "boolean",
                        "description": "true si hay que cambiar cuándo se ejecuta la tarea. false si solo cambia título/instrucción o nada.",
                    },
                    "nuevos_dias_para_vencer": DIAS_OFFSET_SCHEMA,
                    "nuevas_horas_para_vencer": HORAS_OFFSET_SCHEMA,
                    "nota_bitacora": NOTA_BITACORA_SCHEMA,
                    "hito_resuelto": HITO_RESUELTO_SCHEMA,
                },
                "required": [
                    "razonamiento", "temperatura", "id_tarea_a_actualizar",
                    "nuevo_titulo", "nueva_instruccion", "reagendar",
                    "nuevos_dias_para_vencer", "nuevas_horas_para_vencer", "nota_bitacora",
                ],
            },
        },
    }


PROMPT_FRAGMENT = """
OPCIÓN D: `actualizar_tarea_existente`
Úsala cuando ya existe una tarea abierta (ver TAREAS PENDIENTES YA ABIERTAS arriba) cuyo objetivo sigue siendo el mismo — por ejemplo, el cliente pidió reagendar el contacto para otro momento, no respondió y toca reintentar más tarde, o dio un dato nuevo que ajusta la tarea sin cambiar su propósito de fondo. NO crees una tarea nueva ni elimines la anterior en estos casos.
- Debes indicar `id_tarea_a_actualizar` con el ID EXACTO de la tarea a modificar, tomado de la lista de TAREAS PENDIENTES YA ABIERTAS. Nunca inventes un ID.
- Si además cambió el objetivo o el mensaje a enviar, completa también `nuevo_titulo` y/o `nueva_instruccion` (respetando REGLA DE TÍTULOS DE TAREA y las REGLAS DE ORO); si no cambian, déjalos como string vacío "".
- Indica `reagendar` = true si corresponde cambiar cuándo se ejecuta la tarea. Si es true, completa `nuevos_dias_para_vencer` y `nuevas_horas_para_vencer` con el mismo criterio de la REGLA DE FECHA Y HORA DE LA TAREA y la REGLA DE PLAZO MÍNIMO SEGÚN TEMPERATURA. Si es false, completa igual ambos campos con cualquier valor válido (no se usarán) — SALVO que la tarea ya esté vencida (su fecha de vencimiento actual ya pasó respecto a HORA ACTUAL): en ese caso `reagendar` DEBE ser true, sin excepción, ya que una tarea no puede quedar con vencimiento en el pasado.
- Debes asignar `temperatura` igual que en las demás opciones.
"""


def _tarea_esta_vencida(tareas_pendientes_ghl: list[dict], ghl_task_id: str, ahora_miami: datetime) -> bool:
    """Chequea si la tarea a actualizar ya venció, comparando su fecha_limite actual contra 'ahora'."""
    tarea = next((t for t in tareas_pendientes_ghl if str(t.get("id")) == str(ghl_task_id)), None)
    if not tarea:
        return False
    fecha_raw = tarea.get("fecha_limite")
    if not fecha_raw:
        return False
    try:
        fecha_limite = datetime.fromisoformat(fecha_raw)
        if fecha_limite.tzinfo is None:
            fecha_limite = fecha_limite.replace(tzinfo=MIAMI_TZ)
        return fecha_limite < ahora_miami
    except (TypeError, ValueError) as e:
        logger.debug(f"No se pudo parsear fecha_limite={fecha_raw!r} de la tarea {ghl_task_id}: {e}")
        return False


def execute_handler(result: dict, ctx: ExecutionContext) -> dict:
    """
    PASO A: actualiza la tarea en GHL vía API directa (PUT).
    PASO B: dispara el webhook para actualizar los custom fields del contacto.
    """
    ghl_task_id = str(result.get("id_tarea_a_actualizar", ""))

    # --- Validación de ID contra las tareas reales de ESTE contacto ------
    # Antes esto lo garantizaba el enum del schema. Al sacarlo (para
    # mantener el schema estático y no romper el cache_control), esta
    # verificación pasa a ser la única barrera contra un ID inventado
    # por el modelo o, peor, un ID válido pero de OTRO contacto.
    ids_validos = {str(t.get("id")) for t in ctx.tareas_pendientes_ghl if t.get("id")}
    if not ghl_task_id or ghl_task_id not in ids_validos:
        logger.error(
            f"❌ id_tarea_a_actualizar={ghl_task_id!r} inválido para {ctx.contact_name}. "
            f"IDs abiertos reales: {sorted(ids_validos)}"
        )
        raise ValueError(
            f"El modelo devolvió id_tarea_a_actualizar={ghl_task_id!r}, que no está "
            f"entre las tareas abiertas de {ctx.contact_name}."
        )
    # ---------------------------------------------------------------------

    if not ctx.ghl_token:
        logger.error("❌ CRÍTICO: ghl_token es None o vacío. No se puede actualizar la tarea en GHL.")
        raise ValueError("GHL_API_KEY no configurada en el entorno del worker")

    # --- Seguro contra tareas vencidas ---------------------------------
    # No confiamos en que el LLM haya marcado reagendar=true cuando
    # correspondía: si la fecha de vencimiento actual de la tarea ya pasó,
    # forzamos el reagendado igual, server-side.
    reagendar = result.get("reagendar") is True
    if not reagendar and _tarea_esta_vencida(ctx.tareas_pendientes_ghl, ghl_task_id, ctx.ahora_miami):
        logger.warning(
            f"⚠️ Tarea {ghl_task_id} ya estaba vencida y el LLM no marcó reagendar=true. "
            f"Forzando reagendado | {ctx.contact_name}"
        )
        reagendar = True
    # ---------------------------------------------------------------------

    logger.info(f"✏️ [PASO A] Actualizando tarea {ghl_task_id} en GHL vía API directa | {ctx.contact_name}")

    payload_api_tarea = {
        "title": str(result.get("nuevo_titulo", "")),
        "body": str(result.get("nueva_instruccion", "")),
    }
    payload_api_tarea = {k: v for k, v in payload_api_tarea.items() if v}

    nueva_fecha_limite_para_webhook = ""
    if reagendar:
        dias = aplicar_piso_temperatura(
            int(result.get("nuevos_dias_para_vencer", 0)),
            result.get("temperatura", "Frio"),
            ctx.contact_name,
            campo="nuevos_dias_para_vencer",
        )
        try:
            due_date_iso = calcular_due_date(
                dias_para_vencer=dias,
                horas_para_vencer=int(result.get("nuevas_horas_para_vencer", 0)),
                ahora_miami=ctx.ahora_miami,
            )
            payload_api_tarea["dueDate"] = due_date_iso
            nueva_fecha_limite_para_webhook = due_date_iso
        except (TypeError, ValueError) as e:
            logger.error(f"❌ Error calculando dueDate para API | {ctx.contact_name}: {e}")

    with httpx.Client() as client:
        response_tarea = client.put(
            f"{GHL_BASE_URL}/contacts/{ctx.contact_id}/tasks/{ghl_task_id}",
            headers={
                "Authorization": f"Bearer {ctx.ghl_token}",
                "Version": "2021-07-28",
                "Content-Type": "application/json",
            },
            json=payload_api_tarea,
            timeout=15.0,
        )
        response_tarea.raise_for_status()
        logger.info(f"✅ Tarea {ghl_task_id} actualizada exitosamente en GHL | {ctx.contact_name}")

    webhook_payload = {
        "task_id": ctx.task_id,
        "contact_id": ctx.contact_id,
        "contact_name": ctx.contact_name,
        "accion": "actualizar_tarea",
        "ghl_task_id": ghl_task_id,
        "temperatura": result.get("temperatura", "No determinado"),
        "task_title": result.get("nuevo_titulo") or "",
        "task_body": result.get("nueva_instruccion") or "",
        "nueva_fecha_limite": nueva_fecha_limite_para_webhook,
        "razonamiento": result.get("razonamiento", ""),
        "nota_bitacora": ctx.nota_bitacora,
        "custom_field_value": ctx.fecha_hora_revision,
    }

    logger.info(f"🚀 [PASO B] Disparando webhook para actualizar custom fields | {ctx.contact_name}")
    webhook_response = httpx.post(ctx.webhook_url, json=webhook_payload, timeout=15.0)
    webhook_response.raise_for_status()
    resp_json = webhook_response.json()
    trigger_id = resp_json.get("triggerId") or resp_json.get("id") or "N/A"
    logger.info(f"✅ Webhook OK (custom fields actualizados) | {ctx.contact_name} | trigger_id={trigger_id}")

    return {"status": "success", "ghl_task_id": ghl_task_id, "trigger_id": trigger_id}