"""
Tasks dispatcher.

En producción los jobs se encolan en Redis para que el worker los ejecute.
En modo SYNC_MODE=true ejecuta directo (útil para tests o ambientes sin Redis).
"""

import os
import logging
import httpx
import json
from datetime import datetime
from typing import Dict, Any
from openai import OpenAI

from .redis_queue import get_queue, QUEUE_HIGH, QUEUE_AI, get_redis
from .jobs import job_classify_user_preference, job_general_chat
from .agent_state import AgentStateManager
from .entity_resolver import EntityResolver
from .intent_classifier import IntentClassifier
from .templates import get_template
from .catalog_cache import CatalogCache

# ============================================
# CONFIGURACIÓN
# ============================================

logger = logging.getLogger(__name__)
SYNC_MODE = os.getenv("SYNC_MODE", "false").lower() == "true"
DEBUG = os.getenv("DEBUG", "false").lower() == "true"

# ============================================
# CONFIGURACIÓN DE CATÁLOGO
# ============================================

INTENCIONES_CON_CATALOGO = ['intencion_compra_repuestos', 'consulta_disponibilidad']
CATALOG_ENDPOINT = os.getenv("CATALOG_ENDPOINT", "http://backend:8000/products/internal/resolve-by-entities")
CATALOG_TIMEOUT = float(os.getenv("CATALOG_TIMEOUT", "3.0"))

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
# TAREAS PARA GHL
# ============================================

def send_message_to_ghl(contact_id: str, message: str, channel: str = "WhatsApp") -> Dict[str, Any]:
    """
    Envía la respuesta generada por el LLM de vuelta al contacto en GHL
    usando la Conversations API (Send a new message).
    """
    token = os.getenv("GHL_PRIVATE_TOKEN")
    if not token:
        raise ValueError("GHL_PRIVATE_TOKEN no configurada")

    url = "https://services.leadconnectorhq.com/conversations/messages"
    headers = {
        "Authorization": f"Bearer {token}",
        "Version": "2021-04-15",
        "Content-Type": "application/json",
    }
    payload = {
        "type": channel,
        "contactId": contact_id,
        "message": message,
    }

    resp = httpx.post(url, json=payload, headers=headers, timeout=10)
    resp.raise_for_status()
    return resp.json()

def resolver_y_responder_catalogo(state, contact_id, intencion, channel):
    """
    Resuelve producto+modelo vía endpoint y responde directamente a GHL.
    
    Args:
        state: Estado actual del usuario (de Redis)
        contact_id: ID del contacto en GHL
        intencion: Intención detectada
        channel: Canal de comunicación (WhatsApp, etc.)
    
    Returns:
        Dict con resultado o None si falla (para degradar a LLM)
    """
    try:
        producto = state.get('producto')
        modelo = state.get('modelo')
        
        if not producto or not modelo:
            logger.warning(f"⚠️ Faltan producto o modelo para {contact_id}")
            return None
        
        logger.info(f"🔍 Resolviendo catálogo: producto='{producto}', modelo='{modelo}'")
        
        # ✅ Llamada al endpoint con timeout
        start_time = datetime.now()
        
        with httpx.Client(timeout=CATALOG_TIMEOUT) as client:
            response = client.post(
                CATALOG_ENDPOINT,
                json={
                    "identidad_producto": producto,
                    "identidad_modelo": modelo
                }
            )
            response.raise_for_status()
            data = response.json()
        
        elapsed_ms = int((datetime.now() - start_time).total_seconds() * 1000)
        
        # ✅ Seleccionar plantilla según resultado
        if data.get("found"):
            template_key = "found"
            url = data.get("url", "")
            respuesta = get_template(intencion, template_key).format(url=url)
            logger.info(f"✅ Producto encontrado: {data.get('stockid')} en {elapsed_ms}ms")
        else:
            template_key = "not_found"
            modelo_nombre = data.get("modelo", modelo)
            url = data.get("url", "")
            respuesta = get_template(intencion, template_key).format(
                modelo=modelo_nombre,
                url=url
            )
            logger.info(f"⚠️ Producto NO encontrado, fallback a catálogo en {elapsed_ms}ms")
        
        # ✅ Enviar a GHL
        send_message_to_ghl(contact_id, respuesta, channel)
        logger.info(f"📤 Respuesta de catálogo enviada a GHL")
        
        # ✅ Actualizar estado
        state_manager = AgentStateManager()
        state_manager.add_turno(contact_id, f"[catalogo:{intencion}]", respuesta)
        state_manager.update_state(contact_id, {
            "ultima_intencion": intencion,
            "entidades_no_resueltas": []  # Limpiar estado obsoleto
        })
        
        return {
            "success": True,
            "response": respuesta,
            "contact_id": contact_id,
            "intencion": intencion,
            "catalog_resolved": True,
            "catalog_found": data.get("found", False),
            "processed_at": datetime.now().isoformat()
        }
        
    except httpx.TimeoutException:
        logger.error(f"⏰ Timeout en resolve-by-entities para {contact_id} (timeout={CATALOG_TIMEOUT}s)")
        return None
        
    except httpx.HTTPStatusError as e:
        logger.error(f"❌ HTTP {e.response.status_code} en resolve-by-entities: {e.response.text[:200]}")
        return None
        
    except (KeyError, json.JSONDecodeError) as e:
        logger.error(f"❌ Respuesta malformada del endpoint: {e}")
        return None
        
    except Exception as e:
        logger.error(f"❌ Error inesperado en resolución de catálogo: {str(e)}")
        import traceback
        logger.error(traceback.format_exc())
        return None

def process_ghl_message(task_data: Dict[str, Any]) -> Dict[str, Any]:
    """Procesa el mensaje de GHL con LLM + ENTIDADES ESTRUCTURADAS"""
    
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
            "email": task_data.get('email', '')
        }
        
        state = state_manager.get_state(contact_id)
        if not state:
            logger.info(f"🆕 Nuevo contacto, inicializando...")
            state = state_manager.initialize_state(contact_id, contact_data)
        else:
            logger.info(f"📦 Estado recuperado de Redis")

        # ============================================
        # 3. RESOLVER ENTIDADES DEL MENSAJE
        # ============================================
        resolver = EntityResolver()
        resolution = resolver.resolve_entities(message, contact_id)

        # ✅ ACTUALIZAR ESTADO CON ENTIDADES RESUELTAS
        if resolution['resolved']:
            state_manager.update_state(contact_id, resolution['resolved'])
            # Recargar estado actualizado
            state = state_manager.get_state(contact_id)

        if resolution['resolved']:
            logger.info(f"🔍 Entidades resueltas: {resolution['resolved']}")
        else:
            logger.info(f"🔍 Entidades resueltas: Ninguna")

        # ============================================
        # 4. CLASIFICAR INTENCIÓN
        # ============================================
        intent_classifier = IntentClassifier()
        catalog_cache = CatalogCache()

        intenciones_disponibles = catalog_cache.get_intenciones()
        intenciones_texto = "\n".join(
            f"- {i['nombre']}: {i['descripcion']}" for i in intenciones_disponibles
        )

        intent_prompt = f"""
        Clasifica la intención del siguiente mensaje de un cliente de una tienda de motos.

        Mensaje: "{message}"

        Intenciones posibles:
        {intenciones_texto}

        Responde SOLO con el nombre de la intención, sin explicación.
        """

        client = OpenAI(
            base_url="https://openrouter.ai/api/v1",
            api_key=os.getenv("OPENROUTER_API_KEY"),
        )

        intent_response = client.chat.completions.create(
            model="openai/gpt-4o-mini",
            messages=[{"role": "user", "content": intent_prompt}],
            temperature=0.1,
            max_tokens=20,
        )

        intencion = intent_response.choices[0].message.content.strip()
        logger.info(f"🎯 Intención clasificada: {intencion}")

        # ============================================
        # 4.5. PERSISTENCIA DE INTENCIÓN
        # ============================================
        # ✅ Si el mensaje no tiene intención comercial (sin_clasificar)
        if intencion == 'sin_clasificar':
            intencion_activa = state.get('ultima_intencion')
            entidades_pendientes = state.get('entidades_no_resueltas', [])
            
            # ✅ Solo mantener la intención si hay entidades pendientes por resolver
            if intencion_activa and intencion_activa != 'sin_clasificar' and entidades_pendientes:
                intencion = intencion_activa
                logger.info(f"🔄 Manteniendo intención activa: {intencion} (pendientes: {entidades_pendientes})")
            else:
                # ✅ No hay intención activa o ya se completó la conversación
                logger.info(f"ℹ️ No hay entidades pendientes, mensaje fuera de contexto")
                # La intención queda como sin_clasificar
        else:
            # ✅ El mensaje tiene una intención clara, actualizar
            logger.info(f"✅ Nueva intención detectada: {intencion}")

        # ============================================
        # 5. VALIDAR ENTIDADES REQUERIDAS
        # ============================================
        faltantes = intent_classifier.validate_entities(intencion, state)
        
        if faltantes:
            logger.info(f"⚠️ Faltan entidades: {faltantes}")
            
            # ✅ Construir pregunta específica
            producto = state.get('producto')
            if producto and 'producto' not in faltantes:
                # Si el producto ya está resuelto, preguntar solo por el modelo
                respuesta_pregunta = f"¿Para qué modelo de moto necesitas {producto}?"
            else:
                # Si falta el producto también, preguntar por ambos
                respuesta_pregunta = "¿Qué producto o repuesto necesitas y para qué modelo de moto?"
            
            # Guardar estado
            state_manager.update_state(contact_id, {
                "ultima_intencion": intencion,
                "entidades_no_resueltas": faltantes
            })
            
            send_message_to_ghl(contact_id, respuesta_pregunta, channel)
            logger.info(f"📤 Pregunta enviada: {respuesta_pregunta}")
            
            return {
                "success": True,
                "response": respuesta_pregunta,
                "contact_id": contact_id,
                "intencion": intencion,
                "entidades_faltantes": faltantes,
                "processed_at": datetime.now().isoformat()
            }

        # ============================================
        # 5.5. RESOLUCIÓN DE CATÁLOGO (NUEVO)
        # ============================================
        # ✅ Si todas las entidades están resueltas y la intención requiere catálogo
        if intencion in INTENCIONES_CON_CATALOGO:
            logger.info(f"🔍 Resolviendo con catálogo para intención: {intencion}")
            
            catalog_result = resolver_y_responder_catalogo(state, contact_id, intencion, channel)
            
            if catalog_result:
                # ✅ Ya se respondió a GHL dentro de la función
                logger.info(f"✅ Catálogo resuelto exitosamente")
                return catalog_result
            else:
                # ⚠️ El endpoint falló, degradar al LLM
                logger.warning("⚠️ Catálogo falló, degradando al flujo LLM (paso 6)")
                # Continuar al paso 6 (sin cambios en el código existente)
        else:
            logger.info(f"ℹ️ Intención '{intencion}' no requiere catálogo, usando LLM")

        # ============================================
        # 6. PROMPT DEL LLM
        # ============================================
        entidades_texto = ""
        if state.get('modelo'):
            entidades_texto += f"- Modelo de moto: {state['modelo']}\n"
        if state.get('producto'):
            entidades_texto += f"- Producto: {state['producto']}\n"
        if state.get('ultimo_modelo'):
            entidades_texto += f"- Último modelo mencionado: {state['ultimo_modelo']}\n"
        
        turnos = state.get('ultimos_turnos', [])[-3:]
        historial_texto = ""
        if turnos:
            historial_texto = "Historial reciente:\n"
            for t in turnos:
                historial_texto += f"Cliente: {t['cliente']}\n"
                historial_texto += f"Asistente: {t['asistente']}\n\n"

        system_prompt = f"""
        Eres un asistente de ventas de Quinchau Motos, una tienda de motos.
        Tu nombre es Quinchau Assistant.
        El cliente se llama {first_name} {last_name}.

        INFORMACIÓN CONOCIDA DEL CLIENTE:
        {entidades_texto if entidades_texto else "Aún no tenemos información específica del cliente."}

        {historial_texto}

        Reglas:
        - Usa la información conocida del cliente para ser más preciso.
        - Responde de manera amable, profesional y concisa.
        - NO adivines productos o modelos que no estén en la información conocida.
        """

        user_prompt = f"""
        Mensaje del cliente: {message}

        Intención detectada: {intencion}
        """

        # ============================================
        # 7. MOSTRAR PROMPT (SIMPLIFICADO)
        # ============================================
        logger.info("=" * 60)
        logger.info("📝 PROMPT DEL LLM:")
        logger.info("-" * 40)
        logger.info(f"🧠 System: {system_prompt.strip()[:200]}...")
        logger.info(f"👤 User: {message}")
        logger.info("=" * 60)

        # ============================================
        # 8. GENERAR RESPUESTA
        # ============================================
        logger.info("🔄 Generando respuesta...")
        
        response = client.chat.completions.create(
            model="openai/gpt-4o-mini",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}
            ],
            temperature=0.3,
            max_tokens=300,
        )

        llm_response = response.choices[0].message.content

        # ============================================
        # 9. MOSTRAR RESPUESTA
        # ============================================
        logger.info("=" * 60)
        logger.info("📨 RESPUESTA DEL LLM:")
        logger.info("-" * 40)
        logger.info(llm_response)
        logger.info("=" * 60)

        # ============================================
        # 10. ACTUALIZAR ESTADO
        # ============================================
        state_manager.add_turno(contact_id, message, llm_response)
        
        state_manager.update_state(contact_id, {
            "ultima_intencion": intencion,
            "ultimo_modelo": state.get('modelo')
        })

        # ============================================
        # 11. ENVIAR A GHL
        # ============================================
        send_message_to_ghl(contact_id, llm_response, channel)
        logger.info(f"📤 Respuesta enviada a GHL")

        # ============================================
        # 12. MOSTRAR ESTADO EN REDIS
        # ============================================
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
        
        return {
            "success": True,
            "response": llm_response,
            "contact_id": contact_id,
            "intencion": intencion,
            "entidades_resueltas": resolution['resolved'],
            "entidades_no_resueltas": resolution['no_resueltas'],
            "processed_at": datetime.now().isoformat()
        }

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