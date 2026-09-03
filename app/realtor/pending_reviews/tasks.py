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

MESES_ABREV_ES = {
    1: "ENE", 2: "FEB", 3: "MAR", 4: "ABR", 5: "MAY", 6: "JUN",
    7: "JUL", 8: "AGO", 9: "SEP", 10: "OCT", 11: "NOV", 12: "DIC",
}


def _formatear_fecha_bitacora(dt: datetime) -> str:
    """Ej: '01 SEP 2026'"""
    return f"{dt.day:02d} {MESES_ABREV_ES[dt.month]} {dt.year}"


def _construir_nota_bitacora(cuerpo: str, ahora_miami: datetime) -> str:
    """
    Antepone el encabezado fijo (con la fecha real del sistema) al cuerpo
    generado por el LLM. El encabezado NUNCA lo redacta el modelo, para
    evitar fechas alucinadas o formatos inconsistentes.
    """
    header = f"Bitacora realizada por AI el {_formatear_fecha_bitacora(ahora_miami)}"
    return f"{header}\n\n{cuerpo}" if cuerpo else header


def process_pending_review(payload: dict) -> None:
    """
    Entrypoint del job RQ para Pending Reviews.
    Procesa leads con actividad reciente y oportunidad abierta.
    Delega la actualización del custom field y la creación de la nota de bitácora
    al Workflow de GHL a través del webhook.
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

    # 👉 Instante único de la revisión, en HORA DE MIAMI. Se reutiliza para el
    # custom field y para el encabezado de la bitácora, así ambos quedan consistentes.
    ahora_miami = datetime.now(MIAMI_TZ)

    # Formato MM-DD-YYYY HH:MM:SS (compatible con la mayoría de parsers de GHL)
    fecha_hora_revision = ahora_miami.strftime("%m-%d-%Y %H:%M:%S")

    # 👉 Bitácora: encabezado fijo (construido en código) + cuerpo generado por el LLM
    nota_bitacora = _construir_nota_bitacora(
        cuerpo=result.get("nota_bitacora", ""),
        ahora_miami=ahora_miami,
    )

    # 2. Construir payload para el webhook
    # NOTA: "task_title" y "nota_bitacora" siempre viajan en el payload (poblados o "")
    # en las tres ramas, para que el workflow de GHL nunca reciba una key ausente y
    # falle al mapear.
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
            "nota_bitacora": nota_bitacora,
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
            "nota_bitacora": nota_bitacora,
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
            "nota_bitacora": nota_bitacora,
            "custom_field_value": fecha_hora_revision,
        }

    # 3. Enviar webhook
    logger.info(f"📦 Webhook payload: {webhook_payload}")
    logger.info(f"🚀 Disparando webhook | accion={accion} | {contact_name}")

    response = httpx.post(webhook_url, json=webhook_payload, timeout=15)
    response.raise_for_status()

    logger.info(f"✅ Webhook OK | {contact_name} | trigger_id={response.json().get('id')}")