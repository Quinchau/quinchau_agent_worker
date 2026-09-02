import logging
import os
from datetime import date, timedelta

import httpx

from app.realtor.cool_leads.reasoning import analyze_lead

logger = logging.getLogger(__name__)

SUGGESTION_PREFIX = "[🤖 Sugerencia]"
ACCIONES_VALIDAS = {"crear_tarea", "descartar"}
DUE_DATE_HORA = "09:00 AM"  # hora fija para el Due de las tareas sugeridas


def _ensure_suggestion_prefix(title: str) -> str:
    """
    Garantiza que el título de la tarea comience con el prefijo de sugerencia AI.
    Esto permite:
    1. Que el vendedor distinga visualmente tareas generadas por el sistema.
    2. Rollback masivo en GHL filtrando por este prefijo.
    3. Medición de efectividad (tareas AI completadas vs ignoradas).
    """
    if not title:
        return SUGGESTION_PREFIX
    if title.startswith(SUGGESTION_PREFIX):
        return title
    return f"{SUGGESTION_PREFIX} {title}"


def _build_due_date(dias_seguimiento: int) -> str:
    """
    Calcula el Due Date ya formateado como lo exige GHL (MM-DD-YYYY HH:MM AM/PM).

    IMPORTANTE: el workflow de GHL mapea el campo DUE DATE directamente desde el
    payload del webhook, sin ningún cálculo intermedio. Antes se le pasaba
    `dias_seguimiento` (un entero crudo) y GHL, al no poder parsearlo como fecha,
    caía a epoch 0 → "Dec 31 1969, 7:00 PM (EDT)". Por eso acá resolvemos la
    fecha final nosotros mismos, en el formato exacto que GHL espera.

    - reactivacion: hoy + dias_seguimiento (mínimo 1 día).
    - marcar_lost: no hay "seguimiento" real (el LLM suele mandar 0), pero la
      tarea igual necesita un Due válido y cercano → hoy + 1 día.
    """
    dias_para_due = dias_seguimiento if dias_seguimiento > 0 else 1
    due_date_dt = date.today() + timedelta(days=dias_para_due)
    return f"{due_date_dt.strftime('%m-%d-%Y')} {DUE_DATE_HORA}"


def process_cool_lead(payload: dict) -> None:
    """
    Entrypoint del job RQ.
    Modo B (§7.7): no atrapamos excepciones acá — si algo falla, el job debe quedar visible
    en FailedJobRegistry para reintento o revisión manual.

    - accion="crear_tarea": SIEMPRE se crea una tarea visible para el vendedor (ya sea
      para reactivar el lead o para sugerirle marcarlo como Lost, según tipo_tarea).
    - accion="descartar": NO se crea tarea. El vendedor no necesita hacer nada; solo
      se deja registro del motivo para auditoría.
    """
    webhook_url = os.environ.get("COOL_LEADS_WEBHOOK_URL")
    if not webhook_url:
        raise RuntimeError("COOL_LEADS_WEBHOOK_URL no está seteada en el entorno del Worker.")

    lead_id = payload.get("cool_lead_id") or payload.get("task_id", "unknown_id")

    try:
        contact_id = payload["contact_id"]
        contact_name = payload["contact_name"]
    except KeyError as e:
        logger.error(f"❌ Payload incompleto | lead_id={lead_id} | falta clave={e} | payload={payload}")
        raise

    logger.info(f"📩 Job recibido | contact_id={contact_id} | lead_id={lead_id}")

    result = analyze_lead(
        contact_name=contact_name,
        days_inactive=payload["days_inactive"],
        total_messages=payload["total_messages"],
        historial_texto=payload["historial_texto"],
    )

    logger.info(f"🧠 Razonamiento LLM | {contact_name}: {result.get('razonamiento', 'N/A')}")

    accion = result.get("accion")
    if accion not in ACCIONES_VALIDAS:
        logger.warning(f"⚠️ Acción inesperada del LLM: {accion!r} | lead_id={lead_id} | {contact_name}")
        accion = "descartar"

    dias_seguimiento_raw = result.get("dias_para_seguimiento", 0)
    try:
        dias_seguimiento = max(0, min(30, int(dias_seguimiento_raw)))
    except (TypeError, ValueError):
        logger.warning(f"⚠️ dias_para_seguimiento no numérico: {dias_seguimiento_raw!r} | {contact_name}")
        dias_seguimiento = 0

    tipo_tarea = result.get("tipo_tarea", "")

    if accion == "crear_tarea":
        # Cubre ambos casos: reactivación y sugerencia de marcar Lost.
        # El contenido siempre viene del LLM — no se hardcodea texto acá.
        final_title = _ensure_suggestion_prefix(result.get("task_title", ""))
        final_body = result.get("task_body") or "Sin cuerpo especificado por el LLM."
        if not result.get("task_title") or not result.get("task_body"):
            logger.warning(
                f"⚠️ task_title/task_body vacío para accion=crear_tarea | "
                f"tipo_tarea={tipo_tarea} | {contact_name}"
            )
        due_date = _build_due_date(dias_seguimiento)
    else:
        # descartar: no se crea tarea. Campos de tarea vacíos a propósito;
        # el workflow de GHL no debe consumirlos en esta rama.
        final_title = ""
        final_body = ""
        due_date = ""

    webhook_payload = {
        "cool_lead_id": lead_id,
        "contact_id": contact_id,
        "contact_name": contact_name,
        "accion": accion,  # "crear_tarea" o "descartar"
        "tipo_tarea": tipo_tarea,  # "reactivacion" | "marcar_lost" | "" (si descartar)
        "nivel_interes": result.get("nivel_interes", "desconocido"),
        "task_title": final_title,
        "task_body": final_body,
        "motivo_descarte": result.get("motivo", ""),
        "dias_seguimiento": dias_seguimiento,  # se conserva por compatibilidad/debug
        "due_date": due_date,  # <-- nuevo: ya formateado para GHL (MM-DD-YYYY HH:MM AM/PM)
        "custom_field_value": date.today().strftime("%m-%d-%Y"),
    }

    logger.info(f"📦 Webhook payload: {webhook_payload}")
    logger.info(f"🚀 Disparando webhook | accion={accion} | tipo_tarea={tipo_tarea} | {contact_name}")

    response = httpx.post(webhook_url, json=webhook_payload, timeout=15)
    response.raise_for_status()

    logger.info(f"✅ Webhook OK | {contact_name} | trigger_id={response.json().get('id')}")