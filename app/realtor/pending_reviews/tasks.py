import logging
import os
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx

from app.realtor.pending_reviews.reasoning import analyze_pending_review

logger = logging.getLogger(__name__)

ACCIONES_VALIDAS = {"generar_tarea_inmediata", "ia_puede_continuar", "marcar_sin_accion"}

# 👉 Zona horaria de Miami (maneja automáticamente EST/EDT)
MIAMI_TZ = ZoneInfo("America/New_York")


def process_pending_review(payload: dict) -> None:
    """
    Entrypoint del job RQ para Pending Reviews.
    Procesa leads con actividad reciente y oportunidad abierta.
    Delega la actualización del custom field al Workflow de GHL a través del webhook.
    """
    webhook_url = os.environ.get("PENDING_REVIEWS_WEBHOOK_URL")
    if not webhook_url:
        raise RuntimeError("PENDING_REVIEWS_WEBHOOK_URL no está seteada en el entorno del Worker.")

    task_id = payload.get("task_id", "unknown_id")

    try:
        contact_id = payload["contact_id"]
        contact_name = payload["contact_name"]
        historial_texto = payload["historial_texto"]
    except KeyError as e:
        logger.error(f"❌ Payload incompleto | task_id={task_id} | falta clave={e}")
        raise

    logger.info(f"📩 Job recibido | contact_id={contact_id} | task_id={task_id}")

    # 1. Analizar con el LLM
    result = analyze_pending_review(
        contact_name=contact_name,
        historial_texto=historial_texto,
    )

    logger.info(f"🧠 Razonamiento LLM | {contact_name}: {result.get('razonamiento', 'N/A')}")

    accion = result.get("accion")
    if accion not in ACCIONES_VALIDAS:
        logger.warning(f"⚠️ Acción inesperada del LLM: {accion!r} | task_id={task_id} | {contact_name}")
        accion = "marcar_sin_accion"

    # 👉 Fecha y hora exacta de la revisión de la IA en HORA DE MIAMI
    # Formato MM-DD-YYYY HH:MM:SS (compatible con la mayoría de parsers de GHL)
    fecha_hora_revision = datetime.now(MIAMI_TZ).strftime("%m-%d-%Y %H:%M:%S")

    # 2. Construir payload para el webhook
    # NOTA: "task_title" siempre viaja en el payload (poblado o "") en las tres ramas,
    # para que el workflow de GHL nunca reciba la key ausente y falle al mapear.
    if accion == "generar_tarea_inmediata":
        webhook_payload = {
            "task_id": task_id,
            "contact_id": contact_id,
            "contact_name": contact_name,
            "accion": "crear_tarea",
            "temperatura": result.get("temperatura", "No determinado"),
            "interes": result.get("interes", "No determinado"),
            "calificacion_lead": result.get("calificacion_lead", 5),
            "task_title": result.get("titulo_tarea", ""),
            "task_body": result.get("instruccion_para_agente", ""),
            "razonamiento": result.get("razonamiento", ""),
            "custom_field_value": fecha_hora_revision,
        }
    elif accion == "ia_puede_continuar":
        temperatura = result.get("temperatura", "No determinado")
        # Enriquecemos el razonamiento con la temperatura para que el agente tenga contexto rápido en GHL
        razonamiento_con_temperatura = f"Temperatura: {temperatura}\n\n{result.get('razonamiento', '')}"

        webhook_payload = {
            "task_id": task_id,
            "contact_id": contact_id,
            "contact_name": contact_name,
            "accion": "ia_continua",
            "task_title": result.get("titulo_tarea", ""),  # Sugerencia libre del LLM, o "" si no aplica
            "razonamiento": razonamiento_con_temperatura,
            "custom_field_value": fecha_hora_revision,
        }
    else:  # marcar_sin_accion
        webhook_payload = {
            "task_id": task_id,
            "contact_id": contact_id,
            "contact_name": contact_name,
            "accion": "descartar",
            "task_title": "",  # Nunca aplica una sugerencia aquí
            "motivo": result.get("motivo", ""),
            "razonamiento": result.get("razonamiento", ""),
            "custom_field_value": fecha_hora_revision,
        }

    # 3. Enviar webhook
    logger.info(f"📦 Webhook payload: {webhook_payload}")
    logger.info(f"🚀 Disparando webhook | accion={accion} | {contact_name}")

    response = httpx.post(webhook_url, json=webhook_payload, timeout=15)
    response.raise_for_status()

    logger.info(f"✅ Webhook OK | {contact_name} | trigger_id={response.json().get('id')}")