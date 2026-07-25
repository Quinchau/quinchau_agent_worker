import os
import json
import copy
import re
import logging
import io
import httpx
from .ghl import send_message_to_ghl
from datetime import datetime
from typing import Dict, Any, List, Optional

from openai import OpenAI

from .redis_queue import get_queue, QUEUE_HIGH, QUEUE_AI, get_redis
from .jobs import job_classify_user_preference, job_general_chat
from .agent_state import AgentStateManager
from .entity_resolver import entity_resolver
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
# VALOR_SIN_MATCH_PRODUCTO = "ninguno_coincide"


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
# HELPERS DEL NUEVO FLUJO
# ============================================

def _inyectar_razonamiento(herramientas: List[Dict]) -> List[Dict]:
    """
    Agrega un campo 'razonamiento' obligatorio, como primera propiedad,
    al schema de cada tool — para forzar que el modelo articule el tema
    real del mensaje antes de comprometerse con la selección de función.

    Con tool_choice="required", el modelo solo puede responder con
    argumentos de función (no hay canal de texto libre). Sin este campo,
    el modelo salta directo de "hay un producto referenciado" a elegir
    intencion_compra, sin distinguir si la pregunta es sobre el producto
    o sobre el proceso de compra (pago, envío, etc.) que solo lo
    menciona de forma incidental.
    """
    herramientas_parcheadas = copy.deepcopy(herramientas)

    for tool in herramientas_parcheadas:
        params = tool['function']['parameters']
        propiedades_originales = params.get('properties', {})

        params['properties'] = {
            "razonamiento": {
                "type": "string",
                "description": (
                    "Antes de completar el resto de los campos, explica en "
                    "una frase: (1) a qué se refiere el mensaje actual dado "
                    "el historial de la conversación, y (2) cuál es el tema "
                    "real de la pregunta — ¿es sobre el producto en sí "
                    "(disponibilidad, precio, variante), o sobre el proceso "
                    "de compra (pago, envío, horario, garantía)? Sé "
                    "explícito sobre esta distinción incluso si un producto "
                    "está implícito o referenciado en la oración (ej. por "
                    "un pronombre como 'la'/'lo')."
                ),
            },
            **propiedades_originales,
        }
        params['required'] = ["razonamiento"] + params.get('required', [])

    return herramientas_parcheadas

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


def _normalizar_alias(texto: str, alias: Optional[str], modelo: str) -> str:
    if not texto or not alias:
        return texto

    patron = r'\b' + _patron_insensible_a_acentos(alias) + r'\b'
    return re.sub(patron, modelo, texto, flags=re.IGNORECASE)


def _llamar_llm_tool_calling(
    client: OpenAI,
    message: str,
    first_name: str,
    state: Dict,
    historial_texto: str,
    herramientas: List[Dict],
) -> Dict[str, Any]:
    """
    Encapsula una llamada de tool-calling. Se usa tanto para la primera
    pasada como para la segunda (CASO B) — mismo prompt, mismo historial,
    la única diferencia entre llamadas es qué `herramientas` se le pasan
    (con o sin enum de producto poblado).
    """
    herramientas_texto = ""
    for t in herramientas:
        nombre = t['function']['name']
        descripcion = t['function'].get('description', '')
        herramientas_texto += f"- {nombre}: {descripcion}\n"

    system_prompt = load_prompt(
        "prompt_seleccion_herramienta",
        nombre_cliente=first_name,
        modelo=state.get('modelo', 'no especificado'),
        intencion=state.get('ultima_intencion', 'ninguna'),
        historial_texto=historial_texto,
        mensaje=message,
        herramientas_disponibles=herramientas_texto,
    )

    logger.info("📝 PROMPT SELECCIÓN HERRAMIENTA (COMPLETO):")
    logger.info(system_prompt)
    logger.info(f"📝 MENSAJE USUARIO: {message}")
    logger.info(f"📝 TOOLS DISPONIBLES: {[t['function']['name'] for t in herramientas]}")

    tool_response = client.chat.completions.create(
        model="openai/gpt-4o-mini",
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": message},
        ],
        tools=herramientas,
        tool_choice="required",
        temperature=0.0,
    )

    msg = tool_response.choices[0].message
    tool_calls = msg.tool_calls or []

    if tool_calls:
        primera = tool_calls[0]
        intencion = primera.function.name
        try:
            entidades_detectadas = json.loads(primera.function.arguments)
        except json.JSONDecodeError:
            entidades_detectadas = {}
        razonamiento_modelo = entidades_detectadas.pop('razonamiento', '')
        razon = f"Tool seleccionada por el modelo: {intencion} | Razonamiento: {razonamiento_modelo}"
    else:
        intencion = INTENCION_FALLBACK
        entidades_detectadas = {}
        razon = "El modelo no seleccionó ninguna herramienta"

    logger.info(f"🎯 Herramienta seleccionada: {intencion}")
    logger.info(f"🔍 Entidades LLM: {entidades_detectadas}")
    logger.info(f"   Razón: {razon}")

    return {
        "intencion": intencion,
        "entidades_detectadas": entidades_detectadas,
        "razon": razon,
        "tool_calls": tool_calls,
    }


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
    """Procesa el mensaje de GHL:
    FLUJO: Transcripción de audio (si aplica) → Gate 2.5 (modelo, solo
    contexto) → Gate 2.6 (alias de producto) → LLM tool-calling (una sola
    pasada, sin enum de producto) → Handler (cada intención resuelve su
    propia lógica de catálogo/producto si la necesita).
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
        # transcripción de audio como el resto del pipeline más abajo)
        # ============================================
        client = OpenAI(
            base_url="https://openrouter.ai/api/v1",
            api_key=os.getenv("OPENROUTER_API_KEY"),
        )

        # ============================================
        # 1.6 GATE — TRANSCRIPCIÓN DE NOTA DE VOZ (si el mensaje no trajo
        # texto real pero sí un adjunto de audio en customData)
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
        # 2.6 GATE — RESOLVER MODELO (solo contexto, sin catálogo)
        # ============================================
        resultado_gate = entity_resolver.resolver_modelo(message)

        if resultado_gate:
            modelo_resuelto = resultado_gate['modelo']
            alias_usado = resultado_gate['alias']

            state_manager.update_state(contact_id, {
                'modelo': modelo_resuelto,
                'alias_modelo': alias_usado,
                'ultimo_modelo': modelo_resuelto,
                'model_found': True,
                'intentos_resolucion': 0,
                'updated_at': datetime.now().isoformat(),
            })
            state = state_manager.get_state(contact_id)

            logger.info(f"✅ Gate 2.5: modelo '{modelo_resuelto}' (alias '{alias_usado}' normalizado en mensaje)")
        else:
            modelo_resuelto = state.get('modelo')
            alias_usado = state.get('alias_modelo')
            logger.info(f"ℹ️ Gate 2.5: sin match en mensaje, modelo heredado='{modelo_resuelto}'")

        # ============================================
        # 2.7 GATE — RESOLVER ALIAS DE PRODUCTO (solo normalización de mensaje,
        # SIN persistir en state — a diferencia de modelo, producto no es
        # contexto que se herede entre turnos)
        # ============================================
        matches_producto = entity_resolver.resolver_productos_alias(message)

        # Normaliza el alias de modelo (del mensaje actual o heredado del
        # state) y luego los alias de producto detectados — en TODO lo que
        # vaya a viajar hacia un LLM, para que ninguna llamada quede
        # expuesta a jerga/alias crudo.
        message_normalizado = _normalizar_alias(message, alias_usado, modelo_resuelto)
        for m in matches_producto:
            message_normalizado = _normalizar_alias(message_normalizado, m['alias'], m['producto'])

        historial_normalizado = _normalizar_alias(historial_texto, alias_usado, modelo_resuelto)

        # ============================================
        # 4. TOOLS BASE (el cliente OpenAI ya se creó en el paso 1.5)
        # ============================================
        herramientas_base = catalog_cache.get_herramientas()
        herramientas_base = _inyectar_razonamiento(herramientas_base)

        # ============================================
        # 5. LLAMADA AL LLM — solo clasifica intención, sin enum de producto
        # ============================================
        resultado_llm = _llamar_llm_tool_calling(
            client, message_normalizado, first_name, state, historial_normalizado, herramientas_base
        )

        intencion = resultado_llm['intencion']
        entidades_detectadas = resultado_llm['entidades_detectadas']
        razon = resultado_llm['razon']
        tool_calls = resultado_llm['tool_calls']

        # ============================================
        # 6. PERSISTENCIA DE INTENCIÓN
        # ============================================
        intencion_anterior = state.get('ultima_intencion')
        if intencion_anterior and intencion_anterior != intencion:
            logger.info(f"🔄 CAMBIO DE INTENCIÓN: '{intencion_anterior}' → '{intencion}'")

        state_manager.update_state(contact_id, {
            'ultima_intencion': intencion,
            'updated_at': datetime.now().isoformat(),
        })
        state = state_manager.get_state(contact_id)

        # ============================================
        # 7. DESPACHO AL MANEJADOR
        # ============================================
        ctx = IntentContext(
            message=message_normalizado,
            contact_id=contact_id,
            channel=channel,
            first_name=first_name,
            last_name=last_name,
            intencion=intencion,
            confianza=1.0 if tool_calls else 0.0,
            entidades_detectadas=entidades_detectadas,
            razon=razon,
            state=state,
            state_manager=state_manager,
            client=client,
            historial_texto=historial_normalizado,
            resolution={
                'model_found': state.get('model_found', False),
                'modelo': state.get('modelo'),
            },
        )

        manejador = obtener_manejador(intencion)

        if manejador:
            resultado_manejador = manejador(ctx)

            if resultado_manejador and resultado_manejador.get('tool_output'):
                logger.info(f"🔄 Inyección de prompt detectada para: {intencion}")
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
                        f'ultima_respuesta_{intencion}': datetime.now().isoformat(),
                        'tool_output_usado': True,
                        'respuesta_generada_por_llm': True,
                        'esperando_confirmacion': False,
                        'esperando_respuesta': False,
                    })

                    logger.info(f"✅ Respuesta generada y enviada para {intencion}")

                    return {
                        "success": True,
                        "response": respuesta,
                        "contact_id": contact_id,
                        "intencion": intencion,
                        "tool_output_usado": True,
                        "processed_at": datetime.now().isoformat(),
                    }

                except Exception as e:
                    logger.error(f"❌ Error en flujo de inyección: {e}")
                    import traceback
                    logger.error(traceback.format_exc())
                    
                    # Intentar usar el resultado tradicional del manejador como fallback
                    if resultado_manejador is not None and resultado_manejador.get('response'):
                        logger.info("ℹ️ Fallback al resultado tradicional del manejador")
                        state_manager.update_state(contact_id, {
                            'esperando_confirmacion': False,
                            'esperando_respuesta': False,
                        })
                        return resultado_manejador
                    else:
                        # Si no hay respuesta del manejador, enviar mensaje genérico de error
                        mensaje_error = "Lo siento, tuve un problema procesando tu mensaje. ¿Podrías intentarlo de nuevo?"
                        send_message_to_ghl(contact_id, mensaje_error, channel)
                        return {
                            "success": True,
                            "response": mensaje_error,
                            "contact_id": contact_id,
                            "intencion": intencion,
                            "error": True,
                            "processed_at": datetime.now().isoformat(),
                        }

            if resultado_manejador is not None:
                logger.info(f"ℹ️ Flujo tradicional para: {intencion}")
                state_manager.update_state(contact_id, {
                    'esperando_confirmacion': False,
                    'esperando_respuesta': False,
                })
                return resultado_manejador

        else:
            logger.info(f"ℹ️ Intención '{intencion}' no tiene manejador específico")

        # ============================================
        # 8. LLM GENÉRICO (FALLBACK)
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
    # "chat": general_chat_task,
    "process_ghl_message": process_ghl_message,
    "enqueue_ghl_message": enqueue_ghl_message,
}