import logging
import os
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import httpx

from app.realtor.pending_reviews.reasoning import analyze_pending_review

logger = logging.getLogger(__name__)

# ==========================================
# CONFIGURACIÓN PARA LLAMADA DIRECTA A API GHL
# ==========================================
GHL_BASE_URL = "https://services.leadconnectorhq.com"
# Usa la específica, con fallback a la genérica por si el worker usa otro nombre
GHL_API_KEY = os.environ.get("GHL_PRIVATE_TOKEN_FRANCHESCA_QUINTERO_TEAM_MIAMI") or os.environ.get("GHL_PRIVATE_TOKEN")
# ==========================================

ACCIONES_VALIDAS = {
    "generar_tarea_inmediata",
    "ia_puede_continuar",
    "marcar_sin_accion",
    "actualizar_tarea_existente",
}

try:
    MIAMI_TZ = ZoneInfo("America/New_York")
except Exception:
    from datetime import timezone
    MIAMI_TZ = timezone(timedelta(hours=-4))

MESES_ABREV_ES = {
    1: "ENE", 2: "FEB", 3: "MAR", 4: "ABR", 5: "MAY", 6: "JUN",
    7: "JUL", 8: "AGO", 9: "SEP", 10: "OCT", 11: "NOV", 12: "DIC",
}


def _formatear_fecha_bitacora(dt: datetime) -> str:
    """Ej: '01 SEP 2026'"""
    return f"{dt.day:02d} {MESES_ABREV_ES[dt.month]} {dt.year}"


def _construir_nota_bitacora(cuerpo: str, ahora_miami: datetime) -> str:
    header = f"Bitacora realizada por AI el {_formatear_fecha_bitacora(ahora_miami)}"
    return f"{header}\n\n{cuerpo}" if cuerpo else header


def _calcular_due_date(dias_para_vencer: int, hora_limite: str, ahora_miami: datetime) -> str:
    """
    Convierte (dias_para_vencer, hora_limite HH:MM) — los valores relativos que
    devuelve el LLM (nuevos_dias_para_vencer / nueva_hora_limite) — en un dueDate
    absoluto en formato ISO8601, anclado a 'ahora' en hora de Miami.
    El LLM nunca calcula ni escribe la fecha final: solo entrega la distancia en
    días y la hora del día; esta función hace la conversión real.
    Usada para el PUT directo a la API REST de GHL (Paso A, actualizar_tarea_existente).
    """
    hh, mm = map(int, hora_limite.split(":"))
    fecha_objetivo = (ahora_miami + timedelta(days=dias_para_vencer)).replace(
        hour=hh, minute=mm, second=0, microsecond=0
    )
    return fecha_objetivo.isoformat()


def _formatear_due_date_ghl_webhook(dias_para_vencer: int, hora_limite: str, ahora_miami: datetime) -> str:
    """
    Igual que _calcular_due_date pero en el formato que espera el campo
    DUE DATE del Workflow de GHL vía webhook: 'MM-DD-YYYY HH:MM AM/PM'
    (ej. '09-12-2026 10:00 AM'). Solo se usa para generar_tarea_inmediata,
    ya que la creación de tareas pasa por el webhook/Workflow; la actualización
    de tareas existentes va por API directa (_calcular_due_date) y no por acá.
    """
    hh, mm = map(int, hora_limite.split(":"))
    fecha_objetivo = (ahora_miami + timedelta(days=dias_para_vencer)).replace(
        hour=hh, minute=mm, second=0, microsecond=0
    )
    return fecha_objetivo.strftime("%m-%d-%Y %I:%M %p")


def process_pending_review(payload: dict) -> None:
    """
    Entrypoint del job RQ para Pending Reviews.
    Procesa leads con actividad reciente y oportunidad abierta.
    
    Lógica de ejecución:
    - Crear tareas / Otras acciones: Se delegan al Workflow de GHL vía Webhook.
    - Actualizar tareas: Se ejecuta vía API directa de GHL (paso A) y luego 
      se dispara el webhook para actualizar los custom fields del contacto (paso B).
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

    # 👉 Tareas que el Gate ya detectó como abiertas en GHL para este contacto.
    # Se le pasan al LLM para que pueda actualizar una en vez de duplicarla.
    tareas_pendientes_ghl = payload.get("tareas_pendientes_ghl") or []

    logger.info(
        f"📩 Job recibido | contact_id={contact_id} | task_id={task_id} | "
        f"tareas_existentes={len(tareas_pendientes_ghl)}"
    )

    # 1. Analizar con el LLM
    result = analyze_pending_review(
        contact_name=contact_name,
        historial_texto=historial_texto,
        tareas_pendientes_ghl=tareas_pendientes_ghl,
    )

    logger.info(f"🧠 Razonamiento LLM | {contact_name}: {result.get('razonamiento', 'N/A')}")

    accion = result.get("accion")
    if accion not in ACCIONES_VALIDAS:
        logger.warning(f"⚠️ Acción inesperada del LLM: {accion!r} | task_id={task_id} | {contact_name}")
        accion = "marcar_sin_accion"

    # 👉 Salvaguarda: si el LLM eligió actualizar una tarea, el 'enum' del schema
    # ya lo obliga a elegir un ID real (ver reasoning.py), pero validamos igual acá
    # por si el proveedor del modelo no respeta el enum al 100%. Mejor no tocar
    # nada en GHL que arriesgarnos a actualizar la tarea equivocada.
    if accion == "actualizar_tarea_existente":
        ids_validos = {t.get("id") for t in tareas_pendientes_ghl if t.get("id")}
        id_tarea_llm = result.get("id_tarea_a_actualizar", "")
        if id_tarea_llm not in ids_validos:
            logger.warning(
                f"⚠️ id_tarea_a_actualizar={id_tarea_llm!r} inválido (no está en las tareas "
                f"existentes) | task_id={task_id} | {contact_name}. Se descarta la actualización."
            )
            accion = "marcar_sin_accion"

    ahora_miami = datetime.now(MIAMI_TZ)
    fecha_hora_revision = ahora_miami.strftime("%m-%d-%Y %H:%M:%S")

    nota_bitacora = _construir_nota_bitacora(
        cuerpo=result.get("nota_bitacora", ""),
        ahora_miami=ahora_miami,
    )

    # 2. Construir payload para el webhook (se usa en todos los casos)
    if accion == "generar_tarea_inmediata":
        # 👉 dias_para_vencer/hora_limite son campos required del schema, así que
        # siempre vienen en result. Se formatean acá porque el webhook (y el nodo
        # "Add task" del Workflow de GHL) espera 'MM-DD-YYYY HH:MM AM/PM', no el
        # ISO8601 que usa el PUT directo de la API REST.
        try:
            nueva_fecha_limite_crear = _formatear_due_date_ghl_webhook(
                dias_para_vencer=int(result.get("dias_para_vencer", 0)),
                hora_limite=result.get("hora_limite", "10:00"),
                ahora_miami=ahora_miami,
            )
        except (TypeError, ValueError) as e:
            logger.error(
                f"❌ No se pudo calcular nueva_fecha_limite a partir de dias_para_vencer="
                f"{result.get('dias_para_vencer')!r} / hora_limite={result.get('hora_limite')!r} "
                f"| {contact_name}: {e}"
            )
            nueva_fecha_limite_crear = ""

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
            "nueva_fecha_limite": nueva_fecha_limite_crear,
        }
    elif accion == "actualizar_tarea_existente":
        webhook_payload = {
            "task_id": task_id,
            "contact_id": contact_id,
            "contact_name": contact_name,
            "accion": "actualizar_tarea",
            "ghl_task_id": result.get("id_tarea_a_actualizar", ""),
            "temperatura": result.get("temperatura", "No determinado"),
            "task_title": result.get("nuevo_titulo") or "",
            "task_body": result.get("nueva_instruccion") or "",
            "nueva_fecha_limite": result.get("nueva_fecha_limite") or "",
            "razonamiento": result.get("razonamiento", ""),
            "nota_bitacora": nota_bitacora,
            "custom_field_value": fecha_hora_revision,
        }
    elif accion == "ia_puede_continuar":
        webhook_payload = {
            "task_id": task_id,
            "contact_id": contact_id,
            "contact_name": contact_name,
            "accion": "ia_continua",
            "temperatura": result.get("temperatura", "No determinado"),
            "task_title": result.get("titulo_tarea", ""),
            "razonamiento": result.get("razonamiento", ""),
            "nota_bitacora": nota_bitacora,
            "custom_field_value": fecha_hora_revision,
        }
    else:  # marcar_sin_accion
        webhook_payload = {
            "task_id": task_id,
            "contact_id": contact_id,
            "contact_name": contact_name,
            "accion": "descartar",
            "temperatura": result.get("temperatura", "No interesado"),
            "task_title": "",
            "motivo": result.get("motivo", ""),
            "razonamiento": result.get("razonamiento", ""),
            "nota_bitacora": nota_bitacora,
            "custom_field_value": fecha_hora_revision,
        }

    # 3. EJECUCIÓN DE ACCIONES
    logger.info(f"📦 Webhook payload preparado: {webhook_payload}")

    if accion == "actualizar_tarea_existente":
        ghl_task_id = result.get("id_tarea_a_actualizar", "")
        logger.info(f"✏️ [PASO A] Actualizando tarea {ghl_task_id} en GHL vía API directa | {contact_name}")

        # 🔍 DEBUG: Verificar que la clave esté cargada correctamente en tiempo de ejecución
        if not GHL_API_KEY:
            logger.error("❌ CRÍTICO: GHL_API_KEY es None o vacío. El worker no tiene acceso al token de GHL.")
            raise ValueError("GHL_API_KEY no configurada en el entorno del worker")
        
        logger.info(f"🔑 Token GHL detectado. Inicio: '{GHL_API_KEY[:15]}...' | Longitud: {len(GHL_API_KEY)}")

        # PASO A: Actualizar la tarea directamente en la API de GHL
        try:
            payload_api_tarea = {
                "title": result.get("nuevo_titulo", ""),
                "body": result.get("nueva_instruccion", ""),
            }

            # 👉 El LLM nunca entrega una fecha absoluta: entrega 'reagendar' (bool)
            # + 'nuevos_dias_para_vencer' (int) + 'nueva_hora_limite' (HH:MM).
            # Solo calculamos y enviamos dueDate cuando reagendar=True.
            if result.get("reagendar"):
                try:
                    payload_api_tarea["dueDate"] = _calcular_due_date(
                        dias_para_vencer=int(result.get("nuevos_dias_para_vencer", 0)),
                        hora_limite=result.get("nueva_hora_limite", "10:00"),
                        ahora_miami=ahora_miami,
                    )
                except (TypeError, ValueError) as e:
                    logger.error(
                        f"❌ No se pudo calcular dueDate a partir de nuevos_dias_para_vencer="
                        f"{result.get('nuevos_dias_para_vencer')!r} / nueva_hora_limite="
                        f"{result.get('nueva_hora_limite')!r} | {contact_name}: {e}"
                    )

            with httpx.Client() as client:
                response_tarea = client.put(
                    f"{GHL_BASE_URL}/contacts/{contact_id}/tasks/{ghl_task_id}",
                    headers={
                        "Authorization": f"Bearer {GHL_API_KEY}",
                        "Version": "2021-07-28",
                        "Content-Type": "application/json",
                    },
                    json=payload_api_tarea
                )
                response_tarea.raise_for_status()
                logger.info(f"✅ Tarea {ghl_task_id} actualizada exitosamente en GHL | {contact_name}")
        except httpx.HTTPStatusError as e:
            logger.error(f"❌ Error HTTP actualizando tarea {ghl_task_id}: {e.response.status_code} | {e.response.text}")
            raise
        except Exception as e:
            logger.error(f"❌ Error inesperado actualizando tarea {ghl_task_id}: {e}", exc_info=True)
            raise

        # PASO B: Disparar webhook para actualizar custom fields del contacto (temperatura, bitácora, etc.)
        logger.info(f"🚀 [PASO B] Disparando webhook para actualizar custom fields del contacto | {contact_name}")
        try:
            response_webhook = httpx.post(webhook_url, json=webhook_payload, timeout=15)
            response_webhook.raise_for_status()
            logger.info(f"✅ Webhook OK (custom fields actualizados) | {contact_name} | trigger_id={response_webhook.json().get('id')}")
        except Exception as e:
            # Si falla el webhook, la tarea YA se actualizó en el Paso A, así que solo logueamos advertencia
            logger.warning(f"⚠️ La tarea se actualizó correctamente, pero falló el webhook de custom fields: {e}")

    else:
        # Para generar_tarea_inmediata, ia_puede_continuar, marcar_sin_accion
        # El flujo original se mantiene 100% intacto.
        logger.info(f"🚀 Disparando webhook | accion={accion} | {contact_name}")
        response = httpx.post(webhook_url, json=webhook_payload, timeout=15)
        response.raise_for_status()
        logger.info(f"✅ Webhook OK | {contact_name} | trigger_id={response.json().get('id')}")