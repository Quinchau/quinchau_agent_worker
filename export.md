# Project Structure

```
app/
  __pycache__/
    __init__.cpython-312.pyc
    jobs.cpython-312.pyc
    main.cpython-312.pyc
    models.cpython-312.pyc
    redis_queue.cpython-312.pyc
    tasks.cpython-312.pyc
  contextos/
    prompt_consulta_ubicacion_horario.txt
    prompt_entidades_faltantes.txt
    prompt_intencion_cotizar_envio.txt
    prompt_intencion_envio_por_delivery.txt
    prompt_intencion_retiro_y_pago_personal.txt
    prompt_intencion_saludo.txt
    prompt_intent_classifier.txt
    prompt_llm_generico_system.txt
    prompt_llm_generico_user.txt
    prompt_orden_sin_despacho.txt
    prompt_seleccion_catalogo.txt
  intenciones/
    __init__.py
    catalogo.py
    compra_al_mayoreo.py
    compra.py
    context.py
    cotizar_envio.py
    envio_por_delivery.py
    generico.py
    orden_sin_despacho.py
    retiro_y_pago_personal.py
    saludo.py
    sin_clasificar.py
    ubicacion_horario.py
  __init__.py
  agent_state.py
  agent.py
  catalog_cache.py
  database.py
  entity_resolver.py
  ghl.py
  intent_classifier.py
  jobs.py
  main.py
  models.py
  prompts.py
  redis_queue.py
  tasks.py
  templates.py
  worker.py
skills/
.env
.gitignore
Dockerfile-agent
export.md
index_products.py
Quinchau_Agent_Indexacion.docx
README.md
reindex_multi_term.py
reindex_multitag.py
reindex_openrouter.py
reindex_with_models.py
reindex_with_tags.py
requirements.txt
```



# Selected Files Content

## app/tasks.py

```py
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
from .intent_classifier import IntentClassifier
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


# ============================================
# TAREAS PÚBLICAS (FastAPI)
# ============================================

async def classify_user_preference_task(data: Dict[str, Any]) -> Dict[str, Any]:
    """Encola clasificación en Redis (cola high) o ejecuta directo en modo sync"""
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
    """Chat: siempre síncrono (respuesta inmediata esperada)"""
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
    """Procesa el mensaje de GHL: resuelve entidades, clasifica intención y
    despacha al manejador correspondiente en intenciones/."""

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
        # 3. RESOLVER ENTIDADES DEL MENSAJE
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
        # 4. CLASIFICAR INTENCIÓN CON LLM
        # ============================================
        intent_classifier = IntentClassifier()
        catalog_cache = CatalogCache()

        intenciones_disponibles = catalog_cache.get_intenciones()
        intenciones_texto = "\n".join(
            f"- {i['nombre']}: {i['descripcion']}" for i in intenciones_disponibles
        )

        contexto_estado = ""
        if state.get('producto'):
            contexto_estado += f"Producto mencionado anteriormente: {state['producto']}\n"
        if state.get('modelo'):
            contexto_estado += f"Modelo mencionado anteriormente: {state['modelo']}\n"
        if state.get('ultima_intencion'):
            contexto_estado += f"Última intención: {state['ultima_intencion']}\n"
        if state.get('entidades_no_resueltas'):
            contexto_estado += f"Entidades pendientes: {state['entidades_no_resueltas']}\n"
        if not contexto_estado:
            contexto_estado = "No hay contexto previo."

        historial_texto = ""
        turnos = state.get('ultimos_turnos', [])
        if turnos:
            historial_texto = "\nHISTORIAL RECIENTE:\n"
            for turno in turnos[-3:]:
                cliente = turno.get('cliente', '')
                asistente = turno.get('asistente', '')
                if cliente or asistente:
                    historial_texto += f"Cliente: {cliente}\n"
                    historial_texto += f"Asistente: {asistente}\n\n"

        client = OpenAI(
            base_url="https://openrouter.ai/api/v1",
            api_key=os.getenv("OPENROUTER_API_KEY"),
        )

        intent_prompt = load_prompt(
            "prompt_intent_classifier",
            historial_texto=historial_texto,
            contexto_estado=contexto_estado,
            message=message,
            intenciones_texto=intenciones_texto,
        )

        logger.info("=" * 60)
        logger.info("📝 PROMPT COMPLETO PARA CLASIFICACIÓN DE INTENCIÓN:")
        logger.info("-" * 40)
        logger.info(intent_prompt)
        logger.info("=" * 60)

        intent_response = client.chat.completions.create(
            model="openai/gpt-4o-mini",
            messages=[{"role": "user", "content": intent_prompt}],
            temperature=0.1,
            max_tokens=150,
            response_format={"type": "json_object"},
        )

        resultado = json.loads(intent_response.choices[0].message.content)

        intencion = resultado.get('intencion', 'sin_clasificar')
        confianza = resultado.get('confianza', 0.0)
        entidades_detectadas = resultado.get('entidades_detectadas', {})
        razon = resultado.get('razon', '')

        logger.info(f"🎯 Intención clasificada: {intencion} (confianza: {confianza:.2f})")
        logger.info(f"   Razón: {razon}")

        # ============================================
        # 4.5 PERSISTENCIA DE INTENCIÓN
        # ============================================

        intencion_anterior = state.get('ultima_intencion')

        if intencion_anterior and intencion_anterior != intencion:
            logger.info(f"🔄 CAMBIO DE INTENCIÓN: '{intencion_anterior}' → '{intencion}'")
            state_manager.update_state(contact_id, {'ultima_intencion': intencion})
            state = state_manager.get_state(contact_id)
            logger.info(f"📦 Contexto mantenido: producto='{state.get('producto')}', modelo='{state.get('modelo')}'")

        # ============================================
        # 5. DESPACHO AL MANEJADOR DE LA INTENCIÓN
        # ============================================

        ctx = IntentContext(
            message=message,
            contact_id=contact_id,
            channel=channel,
            first_name=first_name,
            last_name=last_name,
            intencion=intencion,
            confianza=confianza,
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
            # El manejador devolvió None → prefiere delegar en el LLM genérico
            # (p. ej. compra.py cuando la consulta al catálogo falla)
        else:
            logger.info(f"ℹ️ Intención '{intencion}' no tiene rama específica, usando LLM genérico")

        # ============================================
        # 6. LLM GENÉRICO (fallback o intenciones sin rama propia)
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
    """
    Encola un mensaje de GHL en la cola QUEUE_AI
    Usado por quinchau_agent (FastAPI)
    """
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


# ============================================
# DICCIONARIO DE TAREAS
# ============================================

TASKS = {
    "classify_user_preference": classify_user_preference_task,
    "chat": general_chat_task,
    "process_ghl_message": process_ghl_message,
    "enqueue_ghl_message": enqueue_ghl_message,
}
```

