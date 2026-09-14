import logging
import os
from datetime import datetime
from typing import Any

import httpx

from app.realtor.pending_reviews.reasoning import analyze_pending_review
from app.realtor.shared.scheduling import MIAMI_TZ, construir_nota_bitacora
from app.realtor.shared.context import ExecutionContext
from app.realtor.pending_reviews.tools import (
    generar_tarea_inmediata,
    ia_puede_continuar,
    marcar_sin_accion,
    actualizar_tarea_existente,
)

logger = logging.getLogger(__name__)

GHL_API_KEY = os.environ.get("GHL_PRIVATE_TOKEN_FRANCHESCA_QUINTERO_TEAM_MIAMI") or os.environ.get("GHL_PRIVATE_TOKEN")
GHL_BASE_URL = "https://services.leadconnectorhq.com"

ACCION_A_HANDLER = {
    "generar_tarea_inmediata": generar_tarea_inmediata.execute_handler,
    "ia_puede_continuar": ia_puede_continuar.execute_handler,
    "marcar_sin_accion": marcar_sin_accion.execute_handler,
    "actualizar_tarea_existente": actualizar_tarea_existente.execute_handler,
}


def _actualizar_bitacora_y_watermark_en_ghl(contact_id: str, bitacora: str, watermark: int, api_key: str) -> None:
    """
    Escritura atómica de la bitácora y el watermark en GHL.
    Se ejecuta solo al final del proceso, garantizando consistencia.
    """
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Version": "2021-07-28",
        "Content-Type": "application/json",
    }
    
    # GHL acepta el valor numérico como string en custom fields vía API
    payload = {
        "customFields": [
            {"id": "bitacora_conversacion", "value": bitacora},
            {"id": "bitacora_ai_watermark", "value": str(watermark)}
        ]
    }
    
    url = f"{GHL_BASE_URL}/contacts/{contact_id}"
    response = httpx.put(url, headers=headers, json=payload, timeout=15.0)
    response.raise_for_status()
    logger.info(f"✅ Bitácora y watermark actualizados atómicamente en GHL para {contact_id}")


def process_pending_review(payload: dict[str, Any]) -> None:
    """
    Entrypoint del job RQ para Pending Reviews (Versión Incremental Simplificada).
    
    1) Extrae el texto a sintetizar (el Gate ya envía la conversación completa 
       si la bitácora está vacía, o solo la ventana nueva si ya existe historial).
    2) Pide al LLM que sintetice ÚNICAMENTE ese texto (instrucción única).
    3) Valida la decisión del LLM y ejecuta el handler correspondiente.
    4) El Backend concatena la nueva síntesis con la bitácora histórica fija.
    5) Si todo es exitoso, escribe la bitácora fusionada y el watermark en GHL.
    """
    # --- TEMPORAL: DEBUG (puedes quitarlo después de esta validación) ---
    import json
    logger.info(f"🔍 PAYLOAD RECIBIDO DEL GATE:\n{json.dumps(payload, indent=2, default=str)}")
    # --------------------------------------------------------------------

    webhook_url = os.environ.get("PENDING_REVIEWS_WEBHOOK_URL")
    if not webhook_url:
        raise RuntimeError("PENDING_REVIEWS_WEBHOOK_URL no está seteada en el entorno del Worker.")

    task_id = payload.get("task_id", "unknown_id")
    
    # 🔧 FIX 1: Inicializar variables para los logs de error. 
    # Esto evita el "UnboundLocalError" si el código falla antes de asignarlas.
    contact_name = payload.get("contact_name", "Desconocido")
    accion = "marcar_sin_accion"

    try:
        # 1. Extraer datos del payload
        contact_id = payload["contact_id"]
        contact_name = payload.get("contact_name", contact_name) # Reafirmar por si viene en el payload
        
        # La bitácora histórica fija (para que el Backend la use al final)
        bitacora_entradas_fijas = payload.get("bitacora_entradas_fijas", "")
        
        # El texto que el LLM debe sintetizar (Gate envía TODO o solo lo NUEVO)
        texto_a_sintetizar = payload.get("texto_a_sintetizar") or payload.get("eventos_nuevos_texto", "")
        
        señal_cierre = payload.get("señal_cierre", "seguir")
        watermark_anterior = payload.get("watermark_anterior")
        watermark_candidato = payload.get("watermark_nuevo")
        tareas_pendientes_ghl = payload.get("tareas_pendientes_ghl") or []

        logger.info(
            f"📩 Job recibido | contact_id={contact_id} | task_id={task_id} | "
            f"tareas_existentes={len(tareas_pendientes_ghl)} | señal={señal_cierre}"
        )

        # 2. Preparar el texto para el LLM (Sin concatenar, sin lógica condicional compleja)
        if not texto_a_sintetizar or not texto_a_sintetizar.strip():
            texto_a_sintetizar = "No hay mensajes para sintetizar."

        # 3. Analizar con el LLM (Instrucción única: sintetizar el texto proporcionado)
        # 🔧 FIX 2: Usamos 'historial_texto' como nombre de parámetro porque la función 
        # analyze_pending_review aún lo espera así internamente. Le pasamos el contenido limpio.
        result = analyze_pending_review(
            contact_name=contact_name,
            historial_texto=texto_a_sintetizar,  # <--- Mapeo seguro al parámetro existente
            tareas_pendientes_ghl=tareas_pendientes_ghl,
            señal_cierre=señal_cierre,
        )

        logger.info(f"🧠 Razonamiento LLM | {contact_name}: {result.get('razonamiento', 'N/A')}")

        # 4. Validar la acción devuelta por el LLM
        accion = result.get("accion") # Ahora sí se sobrescribe con la respuesta real del LLM
        if accion not in ACCION_A_HANDLER:
            logger.warning(f"⚠️ Acción inesperada del LLM: {accion!r} | task_id={task_id} | {contact_name}. Forzando 'marcar_sin_accion'.")
            accion = "marcar_sin_accion"
            result = {**result, "motivo": result.get("motivo", "Acción inválida del modelo, descartado por seguridad.")}

        # Salvaguarda: validar que el ID de tarea a actualizar sea real
        if accion == "actualizar_tarea_existente":
            ids_validos = {str(t.get("id")) for t in tareas_pendientes_ghl if t.get("id")}
            id_tarea_llm = str(result.get("id_tarea_a_actualizar", ""))
            if id_tarea_llm not in ids_validos:
                logger.warning(
                    f"⚠️ id_tarea_a_actualizar={id_tarea_llm!r} inválido (no está en las tareas existentes) "
                    f"| task_id={task_id} | {contact_name}. Se descarta la actualización."
                )
                accion = "marcar_sin_accion"
                result = {**result, "motivo": "ID de tarea a actualizar inválido, descartado por seguridad."}

        ahora_miami = datetime.now(MIAMI_TZ)
        fecha_hora_revision = ahora_miami.strftime("%m-%d-%Y %H:%M:%S")

        # 5. LÓGICA DE APERTURA / CIERRE DE LA BITÁCORA (Responsabilidad del Backend)
        nota_nueva_del_llm = result.get("nota_bitacora", "")
        hito_resuelto = result.get("hito_resuelto", False)

        cerrar = (
            señal_cierre == "cerrar"
            or (señal_cierre == "evaluar" and hito_resuelto is True)
        )

        entrada_formateada = construir_nota_bitacora(cuerpo=nota_nueva_del_llm, ahora_miami=ahora_miami)

        if cerrar:
            # Hito cerrado: se fusiona limpio con el historial
            bitacora_final = (
                f"{bitacora_entradas_fijas}\n\n{entrada_formateada}".strip()
                if bitacora_entradas_fijas else entrada_formateada.strip()
            )
            watermark_a_escribir = watermark_candidato   # el watermark SÍ avanza
            logger.info(f"🔒 Hito cerrado. Watermark avanza a {watermark_a_escribir} | {contact_name}")
        else:
            # ✅ FIX: DELIMITADOR CONDICIONAL
            # Solo usamos el delimitador si YA existe un historial previo.
            # Si bitacora_entradas_fijas está vacío (primera ejecución), guardamos solo la nueva entrada.
            if bitacora_entradas_fijas:
                bitacora_final = f"{bitacora_entradas_fijas}\n\n--- RECIENTE ---\n{entrada_formateada}".strip()
            else:
                bitacora_final = entrada_formateada.strip()
                
            # ✅ Si no hay watermark_anterior, usar el candidato para evitar None
            watermark_a_escribir = watermark_anterior if watermark_anterior else watermark_candidato
            logger.info(f"🔓 Hito abierto. Watermark se mantiene en {watermark_a_escribir} | {contact_name}")

        ctx = ExecutionContext(
            task_id=task_id,
            contact_id=contact_id,
            contact_name=contact_name,
            nota_bitacora=bitacora_final,
            fecha_hora_revision=fecha_hora_revision,
            ahora_miami=ahora_miami,
            webhook_url=webhook_url,
            tareas_pendientes_ghl=tareas_pendientes_ghl,
            ghl_token=GHL_API_KEY,
        )

        # 6. Ejecutar handler
        handler = ACCION_A_HANDLER[accion]
        logger.info(f"🚀 Ejecutando handler | accion={accion} | {contact_name}")
        handler(result, ctx)

        # 7. ESCRITURA ATÓMICA AL FINAL (Solo si el handler tuvo éxito)
        if watermark_a_escribir is not None:
            _actualizar_bitacora_y_watermark_en_ghl(
                contact_id=contact_id,
                bitacora=bitacora_final,
                watermark=watermark_a_escribir,
                api_key=GHL_API_KEY
            )
            logger.info(f"✅ Watermark {watermark_a_escribir} escrito en GHL para {contact_name}")
        else:
            logger.warning(f"⚠️ No se proporcionó 'watermark_a_escribir' para {contact_id}.")

    except KeyError as e:
        logger.error(f"❌ Payload incompleto | task_id={task_id} | falta clave={e}")
        raise
    except httpx.HTTPStatusError as e:
        logger.error(
            f"❌ Error HTTP ejecutando accion={accion} | {contact_name}: "
            f"{e.response.status_code} | {e.response.text}"
        )
        raise
    except Exception as e:
        # 🔧 FIX 1 (parte 2): Ahora 'accion' y 'contact_name' siempre tendrán un valor seguro para el log
        logger.error(f"❌ Error inesperado (accion={accion}) | {contact_name}: {e}", exc_info=True)
        raise