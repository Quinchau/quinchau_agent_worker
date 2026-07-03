import os
import json
import logging
from datetime import datetime
from typing import Dict, Any

from openai import OpenAI

from .redis_queue import get_queue, QUEUE_HIGH, QUEUE_AI, get_redis
from .jobs import job_classify_user_preference, job_general_chat
from .agent_state import AgentStateManager
from .entity_resolver import EntityResolver
from .catalog_cache import CatalogCache
from .prompts import load_prompt
from .intenciones import IntentContext, obtener_manejador
from .intenciones import generico

# ============================================
# CONFIGURACIÓN
# ============================================

logger = logging.getLogger(__name__)
SYNC_MODE = os.getenv("SYNC_MODE", "false").lower() == "true"
DEBUG = os.getenv("DEBUG", "false").lower() == "true"

# Umbral simple de "confianza" implícita: si el modelo no llama NINGUNA tool,
# tratamos el mensaje como sin_clasificar (reemplaza tu antiguo campo `confianza`)
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


async def general_chat_task(data: Dict[str, Any]) -> Dict[str, Any]:
    from .agent import agent

    response = agent.general_chat(
        data.get("message", ""),
        data.get("chat_history", []),
    )
    return {"status": "success", "task_type": "chat", "response": response}


# ============================================
# PROCESAMIENTO DE MENSAJES GHL
# ============================================

def process_ghl_message(task_data: Dict[str, Any]) -> Dict[str, Any]:
    """Procesa el mensaje de GHL: resuelve entidades, decide la herramienta
    (tool calling) y despacha al manejador correspondiente en intenciones/."""

    try:
        # ============================================
        # 1. DATOS DEL USUARIO
        # ============================================
        message = task_data.get('message', '')
        first_name = task_data.get('first_name', 'Cliente')
        last_name = task_data.get('last_name', '')
        contact_id = task_data.get('contact_id')
        channel = task_data.get('channel', 'WhatsApp')

        if not contact_id:
            raise ValueError("contact_id no presente en task_data")

        logger.info("=" * 60)
        logger.info(f"📥 Mensaje: {message}")
        logger.info(f"👤 Usuario: {first_name} {last_name} ({contact_id})")
        logger.info("=" * 60)

        # ============================================
        # 2. ESTADO EN REDIS — sin cambios
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
        # 2.5 HISTORIAL DE CONVERSACIÓN — sin cambios
        # ============================================
        historial_texto = task_data.get('historial_texto', '')

        if not historial_texto:
            logger.warning("⚠️ No hay historial_texto en task_data, usando vacío")
        else:
            logger.info(f"📋 Historial recibido: {len(historial_texto)} caracteres")

        # ============================================
        # 3. RESOLVER ENTIDADES DEL MENSAJE — sin cambios
        #    (esto sigue siendo tu fuente de verdad para sinónimos/jerga,
        #    el LLM NO vuelve a extraer estos valores, se los pasamos ya resueltos)
        # ============================================
        resolver = EntityResolver()
        resolution = resolver.resolve_entities(message, contact_id)

        if resolution['resolved']:
            state_manager.update_state(contact_id, resolution['resolved'])
            state = state_manager.get_state(contact_id)
            logger.info(f"🔍 Entidades resueltas: {resolution['resolved']}")
        else:
            logger.info("🔍 Entidades resueltas: Ninguna")

        # ============================================
        # 4. DECIDIR HERRAMIENTA CON TOOL CALLING
        #    (reemplaza al antiguo prompt_intent_classifier + JSON de intención)
        # ============================================
        catalog_cache = CatalogCache()
        herramientas = catalog_cache.get_herramientas()  # ver nota abajo

        client = OpenAI(
            base_url="https://openrouter.ai/api/v1",
            api_key=os.getenv("OPENROUTER_API_KEY"),
        )

        system_prompt = load_prompt(
            "prompt_seleccion_herramienta",
            nombre_cliente=first_name,
            producto=state.get('producto', 'no especificado'),
            modelo=state.get('modelo', 'no especificado'),
            intencion=state.get('ultima_intencion', 'ninguna'),
            historial_texto=historial_texto,
        )

        tool_response = client.chat.completions.create(
            model="openai/gpt-4o-mini",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": message},
            ],
            tools=herramientas,
            tool_choice="required",  # antes "auto": obliga a elegir siempre una
            # tool, incluida sin_clasificar. Con "auto" el modelo podía no
            # llamar ninguna, y "no tool call" quedaba como default implícito
            # compitiendo de forma no determinística con la lógica de 3
            # reintentos de compra.py (ver casos "chirulai"/"chuflin"/"chinchulin").
            temperature=0.1,
        )

        msg = tool_response.choices[0].message
        tool_calls = msg.tool_calls or []

        if tool_calls:
            # Nota: si tu negocio necesita procesar varias tools en un mismo
            # mensaje (ej: "tienes la pipa del GN125 y a qué hora abren"),
            # acá es donde se itera tool_calls en vez de tomar solo la primera.
            primera = tool_calls[0]
            intencion = primera.function.name
            try:
                entidades_detectadas = json.loads(primera.function.arguments)
            except json.JSONDecodeError:
                entidades_detectadas = {}
            razon = f"Tool seleccionada por el modelo: {intencion}"
        else:
            intencion = INTENCION_FALLBACK
            entidades_detectadas = {}
            razon = "El modelo no seleccionó ninguna herramienta"

        logger.info(f"🎯 Herramienta seleccionada: {intencion}")
        logger.info(f"   Entidades: {entidades_detectadas}")
        logger.info(f"   Razón: {razon}")

        # ============================================
        # 4.5 PERSISTENCIA DE INTENCIÓN — sin cambios
        # ============================================
        intencion_anterior = state.get('ultima_intencion')

        if intencion_anterior and intencion_anterior != intencion:
            logger.info(f"🔄 CAMBIO DE INTENCIÓN: '{intencion_anterior}' → '{intencion}'")
            state_manager.update_state(contact_id, {'ultima_intencion': intencion})
            state = state_manager.get_state(contact_id)
            logger.info(f"📦 Contexto mantenido: producto='{state.get('producto')}', modelo='{state.get('modelo')}'")

        # ============================================
        # 5. DESPACHO AL MANEJADOR — SIN CAMBIOS
        #    (obtener_manejador ya usaba el nombre de intención como key,
        #    que ahora es literalmente el mismo string que `tool_call.name`)
        # ============================================
        ctx = IntentContext(
            message=message,
            contact_id=contact_id,
            channel=channel,
            first_name=first_name,
            last_name=last_name,
            intencion=intencion,
            confianza=1.0 if tool_calls else 0.0,  # ya no viene del LLM, se infiere
            entidades_detectadas=entidades_detectadas,
            razon=razon,
            state=state,
            state_manager=state_manager,
            client=client,
            historial_texto=historial_texto,
            resolution=resolution,
        )

        manejador = obtener_manejador(intencion)

        if manejador:
            resultado_manejador = manejador(ctx)
            if resultado_manejador is not None:
                return resultado_manejador
        else:
            logger.info(f"ℹ️ Intención '{intencion}' no tiene rama específica, usando LLM genérico")

        # ============================================
        # 6. LLM GENÉRICO — sin cambios
        # ============================================
        resultado_final = generico.handle(ctx)

        logger.info("=" * 60)
        logger.info("📊 ESTADO EN REDIS (contacto)")
        logger.info("-" * 40)
        estado_actual = state_manager.get_state(contact_id)
        if estado_actual:
            logger.info(f"👤 Nombre: {estado_actual.get('nombre_cliente', 'N/A')}")
            logger.info(f"🏍️ Modelo: {estado_actual.get('modelo', 'N/A')}")
            logger.info(f"📦 Producto: {estado_actual.get('producto', 'N/A')}")
            logger.info(f"🎯 Intención: {estado_actual.get('ultima_intencion', 'N/A')}")
            logger.info(f"📝 Turnos: {estado_actual.get('turno_actual', 0)}")
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
    "chat": general_chat_task,
    "process_ghl_message": process_ghl_message,
    "enqueue_ghl_message": enqueue_ghl_message,
}