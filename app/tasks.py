# app/tasks.py
import os
import json
import re
import logging
import io
import httpx
from .ghl import send_message_to_ghl
from datetime import datetime
from typing import Dict, Any, List, Optional
from .classifier import clasificar

from openai import OpenAI

from .redis_queue import get_queue, QUEUE_HIGH, QUEUE_AI, get_redis
from .jobs import job_classify_user_preference, job_general_chat
from .agent_state import AgentStateManager
from .catalog_cache import catalog_cache
from .prompts import load_prompt
from .intenciones import IntentContext, obtener_manejador
from .intenciones import generico

# ============================================
# CONFIGURACIÓN
# ============================================

logger = logging.getLogger(__name__)
SYNC_MODE = os.getenv("SYNC_MODE", "false").lower() == "true"
DEBUG = os.getenv("DEBUG", "false").lower() == "true"

INTENCION_FALLBACK = "sin_clasificar"


# ============================================
# TAREAS PÚBLICAS (FastAPI) — sin cambios
# ============================================

async def classify_user_preference_task(data: Dict[str, Any]) -> Dict[str, Any]:
    if SYNC_MODE:
        return job_classify_user_preference(data)

    job = get_queue(QUEUE_HIGH).enqueue(
        job_classify_user_preference,
        data,
        job_timeout=60,
        result_ttl=3600,
    )
    return {
        "success": True,
        "status": "queued",
        "job_id": job.id,
        "category": None,
        "message": f"Job encolado — resultado disponible vía /job/{job.id}",
    }


# ============================================
# HELPERS
# ============================================

_ACENTOS = {
    'a': 'aáàâã', 'e': 'eéèê', 'i': 'iíìî',
    'o': 'oóòôõ', 'u': 'uúùû', 'n': 'nñ',
}

def _patron_insensible_a_acentos(palabra: str) -> str:
    """Construye un patrón regex que matchea la palabra sin importar
    tildes/acentos en el texto real (ej. 'instalacion' matchea 'instalación')."""
    partes = []
    for ch in palabra:
        variantes = _ACENTOS.get(ch.lower())
        if variantes:
            partes.append(f'[{variantes}{variantes.upper()}]')
        else:
            partes.append(re.escape(ch))
    return ''.join(partes)


def _validar_termino(texto: Optional[str], tipo: str) -> Optional[str]:
    """
    Validación exact-match O(1) del término devuelto por el clasificador
    contra el set de términos canónicos del catálogo.

    'tipo' es 'modelo' o 'producto' — solo se usa para filtrar en el set
    (get_terminos_set() devuelve ambos tipos mezclados; el término canónico
    al que se mapea ya implica el tipo correcto).

    Devuelve el término canónico si matchea, None si no. No hace matching
    aproximado: el trabajo semántico ya lo hizo el LLM; esto es solo la
    red de seguridad de que el string que devolvió existe en el catálogo
    actual.
    """
    if not texto:
        return None

    from .entity_resolver import entity_resolver
    from .catalog_cache import catalog_cache

    terminos_set = catalog_cache.get_terminos_set()
    normalizado = entity_resolver.normalize_text(texto)

    return terminos_set.get(normalizado)


# ============================================
# PROCESAMIENTO DE MENSAJES GHL
# ============================================

EXTENSIONES_AUDIO = ('.ogg', '.mp3', '.m4a', '.wav', '.aac', '.mpeg', '.mp4')
MENSAJES_CENTINELA_SIN_TEXTO = {"mensaje no encontrado", ""}

EXTENSIONES_IMAGEN = ('.jpg', '.jpeg', '.png', '.webp', '.gif')

def _es_imagen(url: str) -> bool:
    return bool(url) and url.lower().split('?')[0].endswith(EXTENSIONES_IMAGEN)


def _es_audio(url: str) -> bool:
    return bool(url) and url.lower().split('?')[0].endswith(EXTENSIONES_AUDIO)


def _transcribir_audio(url_audio: str, client: OpenAI) -> Optional[str]:
    """
    Descarga el adjunto de audio (nota de voz de WhatsApp vía GHL) y lo
    transcribe con Whisper a través de OpenRouter. Devuelve None si falla,
    para que el caller decida el mensaje de fallback.
    """
    try:
        resp = httpx.get(url_audio, timeout=30.0)
        resp.raise_for_status()

        extension = url_audio.lower().split('?')[0].rsplit('.', 1)[-1]
        audio_file = io.BytesIO(resp.content)
        audio_file.name = f"nota_voz.{extension}"

        transcripcion = client.audio.transcriptions.create(
            model="openai/whisper-1",
            file=audio_file,
            language="es",
        )
        texto = transcripcion.text.strip()
        logger.info(f"🎙️ Nota de voz transcrita: {texto[:80]}...")
        return texto or None
    except Exception as e:
        logger.error(f"❌ Error transcribiendo audio: {e}")
        return None


def process_ghl_message(task_data: Dict[str, Any]) -> Dict[str, Any]:
    """
    Procesa el mensaje de GHL.

    FLUJO:
      1. Datos del usuario
      1.5 Cliente OpenAI
      1.6 Gate — transcripción de audio
      2. Estado en Redis
      2.5 Gate — imagen recibida (deriva sin clasificar)
      2.6 Clasificador unificado (intención + modelo + producto)
      3. Despacho al handler
      4. LLM genérico (fallback)
    """
    try:
        # ============================================
        # 1. DATOS DEL USUARIO
        # ============================================
        message = task_data.get('message', '')
        first_name = task_data.get('first_name', 'Cliente')
        last_name = task_data.get('last_name', '')
        contact_id = task_data.get('contact_id')
        channel = task_data.get('channel', 'WhatsApp')
        historial_texto = task_data.get('historial_texto', '')

        if not contact_id:
            raise ValueError("contact_id no presente en task_data")

        logger.info("=" * 60)
        logger.info(f"📥 Mensaje: {message}")
        logger.info(f"👤 Usuario: {first_name} {last_name} ({contact_id})")
        logger.info("=" * 60)

        # ============================================
        # 1.5 CLIENTE OPENAI (se crea temprano: lo necesita tanto la
        # transcripción de audio como los handlers más abajo)
        # ============================================
        client = OpenAI(
            base_url="https://openrouter.ai/api/v1",
            api_key=os.getenv("OPENROUTER_API_KEY"),
        )

        # ============================================
        # 1.6 GATE — TRANSCRIPCIÓN DE NOTA DE VOZ
        # ============================================
        custom_data = task_data.get('custom_data', {}) or {}
        attachment_url = custom_data.get('attachments') or ''

        if message.strip().lower() in MENSAJES_CENTINELA_SIN_TEXTO and _es_audio(attachment_url):
            logger.info(f"🎙️ Adjunto de audio detectado, transcribiendo — {first_name}")
            texto_transcrito = _transcribir_audio(attachment_url, client)

            if texto_transcrito:
                message = texto_transcrito
                logger.info(f"🔄 Mensaje reemplazado por transcripción: {message[:80]}...")
            else:
                message = "[Nota de voz recibida, no pude transcribirla]"
                logger.warning("⚠️ Transcripción falló, usando mensaje de fallback")

        # ============================================
        # 2. ESTADO EN REDIS
        # ============================================
        state_manager = AgentStateManager()

        contact_data = {
            "first_name": first_name,
            "last_name": last_name,
            "phone": task_data.get('phone', ''),
            "email": task_data.get('email', ''),
        }

        state = state_manager.get_state(contact_id)
        if not state:
            logger.info("🆕 Nuevo contacto, inicializando...")
            state = state_manager.initialize_state(contact_id, contact_data)
        else:
            logger.info("📦 Estado recuperado de Redis")

            if state.get('status_conversacion') == 'paused':
                pausa_hasta = state.get('pausa_hasta')
                pausa_vencida = False

                if pausa_hasta:
                    try:
                        pausa_vencida = datetime.fromisoformat(pausa_hasta) <= datetime.now()
                    except ValueError:
                        logger.warning(f"⚠️ 'pausa_hasta' inválido para {contact_id}: {pausa_hasta!r}")
                        pausa_vencida = True
                else:
                    pausa_vencida = True

                if pausa_vencida:
                    logger.info(f"▶️ Pausa vencida para {contact_id}, reanudando conversación")
                    state_manager.update_state(contact_id, {
                        'status_conversacion': 'active',
                        'pausa_hasta': None,
                    })
                    state = state_manager.get_state(contact_id)
                else:
                    logger.info(f"⏸️ Conversación en pausa para {contact_id}. Mensaje ignorado.")
                    return {
                        "success": True,
                        "ignored": True,
                        "contact_id": contact_id,
                        "status": "paused",
                        "reason": "conversation_paused",
                        "processed_at": datetime.now().isoformat(),
                    }

        # ============================================
        # 2.5 GATE — IMAGEN RECIBIDA (por ahora no se procesa,
        # se deriva sin pasar por clasificación de intención)
        # ============================================
        if message.strip().lower() in MENSAJES_CENTINELA_SIN_TEXTO and _es_imagen(attachment_url):
            logger.info(f"🖼️ Imagen detectada, derivando (sin clasificar intención) — {first_name}")

            mensaje = "Recibí tu imagen, dame un momento que la reviso y te respondo."
            send_message_to_ghl(contact_id, mensaje, channel)

            state_manager.update_state(contact_id, {
                'ultima_intencion': 'imagen_recibida',
                'esperando_confirmacion': False,
                'esperando_respuesta': False,
            })

            return {
                "success": True,
                "response": mensaje,
                "contact_id": contact_id,
                "intencion": "imagen_recibida",
                "fallback": True,
                "fallback_tipo": "imagen_no_procesada",
                "processed_at": datetime.now().isoformat(),
            }

        # ============================================
        # 2.6 CLASIFICADOR UNIFICADO — intención + modelo + producto
        #
        # Reemplaza Gate 2.5 (resolver_modelo) y Gate 2.7
        # (resolver_productos_alias). El LLM clasificador resuelve los
        # tres en una sola pasada contra el diccionario de términos
        # cacheado (classifier:static_block). La validación
        # determinística (exact-match normalizado) ocurre acá mismo,
        # en _validar_termino(), antes de persistir en state.
        #
        # NOTA: message y historial_texto ya NO se normalizan (alias →
        # término canónico) antes de esta llamada. El clasificador
        # resuelve alias semánticamente en el texto original; la
        # normalización previa era un requisito del Gate por regex, no
        # del LLM.
        # ============================================
        resultado_clasificador = clasificar(
            mensaje=message,
            historial_texto=historial_texto,
            modelo_heredado=state.get('modelo'),
        )

        if resultado_clasificador.get('error'):
            logger.warning(
                f"⚠️ Clasificador con error: {resultado_clasificador['error']} "
                f"— conservando estado previo, despachando con intención 'sin_clasificar'"
            )

        intencion_detectada = resultado_clasificador.get('intencion', 'sin_clasificar')

        # ── Validar modelo ────────────────────────────────────────────
        # El LLM devuelve el nombre canónico (ya viene del enum), pero
        # _validar_termino hace el exact-match final como red de
        # seguridad: si el modelo no existe en el catálogo actual
        # (catálogo cambió entre el rebuild del bloque estático y ahora),
        # lo descarta en vez de persistir un valor huérfano.
        modelo_clasificado = _validar_termino(
            resultado_clasificador.get('modelo'),
            tipo='modelo',
        )

        # ── Validar productos ─────────────────────────────────────────
        # producto llega como lista de strings libres (sin enum en el
        # schema). Cada ítem se valida por separado; los que no matcheen
        # exacto contra el catálogo se descartan silenciosamente (se
        # registra en log para alimentar la cola de no-resueltos, §7).
        productos_raw = resultado_clasificador.get('producto', [])
        productos_validados = []
        for p in productos_raw:
            validado = _validar_termino(p, tipo='producto')
            if validado:
                productos_validados.append(validado)
            else:
                logger.info(
                    f"🔎 Término de producto no resuelto (para cola de revisión): "
                    f"'{p}' | mensaje='{message[:60]}'"
                )

        # ── Persistir en state ────────────────────────────────────────
        # Se eliminan alias_modelo, alias_producto, intentos_resolucion,
        # intentos_producto — esos campos eran exclusivos del Gate viejo.
        # es_aclaracion se mantiene pero lo setea el handler, no acá.
        modelo_vigente = modelo_clasificado or state.get('modelo')

        state_manager.update_state(contact_id, {
            'modelo': modelo_vigente,
            'model_found': modelo_vigente is not None,
            'producto': productos_validados,
            'product_found': bool(productos_validados),
            'ultima_intencion': intencion_detectada,
            'updated_at': datetime.now().isoformat(),
        })
        state = state_manager.get_state(contact_id)

        logger.info(
            f"✅ Clasificador | intención={intencion_detectada} | "
            f"modelo={modelo_vigente} | productos={productos_validados} | "
            f"contacto={first_name}"
        )

        # ============================================
        # 3. DESPACHO AL HANDLER
        # ============================================
        intencion_anterior = state.get('ultima_intencion')
        if intencion_anterior and intencion_anterior != intencion_detectada:
            logger.info(f"🔄 CAMBIO DE INTENCIÓN: '{intencion_anterior}' → '{intencion_detectada}'")

        ctx = IntentContext(
            message=message,
            contact_id=contact_id,
            channel=channel,
            first_name=first_name,
            last_name=last_name,
            intencion=intencion_detectada,
            confianza=1.0 if not resultado_clasificador.get('error') else 0.0,
            entidades_detectadas=resultado_clasificador.get('entidades_extraidas', {}),
            razon=f"Clasificador unificado | error={resultado_clasificador.get('error')}",
            state=state,
            state_manager=state_manager,
            client=client,
            historial_texto=historial_texto,
            resolution={
                'model_found': state.get('model_found', False),
                'modelo': state.get('modelo'),
                'producto': state.get('producto'),
                'producto_candidatos': state.get('producto_candidatos'),
                'es_aclaracion': state.get('es_aclaracion', False),
            },
        )

        manejador = obtener_manejador(intencion_detectada)

        if manejador:
            resultado_manejador = manejador(ctx)

            if resultado_manejador and resultado_manejador.get('tool_output'):
                logger.info(f"🔄 Inyección de prompt detectada para: {intencion_detectada}")
                try:
                    from app.intenciones.inyection_prompt_from_tool import inyectar_y_generar

                    respuesta = inyectar_y_generar(
                        tool_output=resultado_manejador['tool_output'],
                        user_message=message,
                        first_name=first_name,
                        history=historial_texto,
                        client=client,
                    )

                    logger.info(f"📤 Enviando respuesta generada: {respuesta[:100]}...")
                    send_message_to_ghl(contact_id, respuesta, channel)

                    state_manager.update_state(contact_id, {
                        'ultima_respuesta': respuesta,
                        f'ultima_respuesta_{intencion_detectada}': datetime.now().isoformat(),
                        'tool_output_usado': True,
                        'respuesta_generada_por_llm': True,
                        'esperando_confirmacion': False,
                        'esperando_respuesta': False,
                    })

                    logger.info(f"✅ Respuesta generada y enviada para {intencion_detectada}")

                    return {
                        "success": True,
                        "response": respuesta,
                        "contact_id": contact_id,
                        "intencion": intencion_detectada,
                        "tool_output_usado": True,
                        "processed_at": datetime.now().isoformat(),
                    }

                except Exception as e:
                    logger.error(f"❌ Error en flujo de inyección: {e}")
                    import traceback
                    logger.error(traceback.format_exc())

                    if resultado_manejador is not None and resultado_manejador.get('response'):
                        logger.info("ℹ️ Fallback al resultado tradicional del manejador")
                        state_manager.update_state(contact_id, {
                            'esperando_confirmacion': False,
                            'esperando_respuesta': False,
                        })
                        return resultado_manejador
                    else:
                        mensaje_error = "Lo siento, tuve un problema procesando tu mensaje. ¿Podrías intentarlo de nuevo?"
                        send_message_to_ghl(contact_id, mensaje_error, channel)
                        return {
                            "success": True,
                            "response": mensaje_error,
                            "contact_id": contact_id,
                            "intencion": intencion_detectada,
                            "error": True,
                            "processed_at": datetime.now().isoformat(),
                        }

            if resultado_manejador is not None:
                logger.info(f"ℹ️ Flujo tradicional para: {intencion_detectada}")
                state_manager.update_state(contact_id, {
                    'esperando_confirmacion': False,
                    'esperando_respuesta': False,
                })
                return resultado_manejador

        else:
            logger.info(f"ℹ️ Intención '{intencion_detectada}' no tiene manejador específico")

        # ============================================
        # 4. LLM GENÉRICO (FALLBACK)
        # ============================================
        resultado_final = generico.handle(ctx)

        state_manager.update_state(contact_id, {
            'esperando_confirmacion': False,
            'esperando_respuesta': False,
        })

        logger.info("=" * 60)
        estado_actual = state_manager.get_state(contact_id)
        if estado_actual:
            logger.info(f"👤 Nombre: {estado_actual.get('nombre_cliente', 'N/A')}")
            logger.info(f"🏍️ Modelo: {estado_actual.get('modelo', 'N/A')}")
            logger.info(f"🎯 Intención: {estado_actual.get('ultima_intencion', 'N/A')}")
        logger.info("=" * 60)
        logger.info("✅ Worker completado")

        return resultado_final

    except Exception as e:
        logger.error(f"❌ Error: {str(e)}")
        import traceback
        logger.error(traceback.format_exc())
        raise


def enqueue_ghl_message(task_data: Dict[str, Any]) -> Dict[str, Any]:
    queue = get_queue(QUEUE_AI)

    job = queue.enqueue(
        process_ghl_message,
        task_data,
        job_timeout=300,
        result_ttl=86400,
        failure_ttl=86400,
    )

    return {
        "success": True,
        "status": "queued",
        "job_id": job.id,
        "message": f"Job encolado en cola AI",
    }


TASKS = {
    "classify_user_preference": classify_user_preference_task,
    "process_ghl_message": process_ghl_message,
    "enqueue_ghl_message": enqueue_ghl_message,
}