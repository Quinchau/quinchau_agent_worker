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
from .aclaracion_gate import es_aclaracion_de_busqueda
from .intenciones.catalogo import resolver_y_responder_catalogo

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
    """Procesa el mensaje de GHL: 
    NUEVO FLUJO: LLM → EntityResolver (búsqueda semántica) → Handler
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

            # Verificar si conversación está en pausa
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
                    # Pausa sin timestamp (registro antiguo) → no bloqueamos indefinidamente
                    pausa_vencida = True

                if pausa_vencida:
                    logger.info(f"▶️ Pausa vencida para {contact_id}, reanudando conversación")
                    state_manager.update_state(contact_id, {
                        'status_conversacion': 'active',
                        'pausa_hasta': None,
                    })
                    state = state_manager.get_state(contact_id)
                    # No hacemos return: el mensaje sigue el flujo normal más abajo
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

            # Verificar si estamos en flujo de discernimiento
            if state.get('esperando_confirmacion') or state.get('esperando_respuesta'):
                logger.info(f"🔄 Continuando flujo de discernimiento para {contact_id}")

        # ============================================
        # 2.5 PUERTA DE CATÁLOGO (DETECCIÓN Y ACTUALIZACIÓN DE ESTADO)
        # Detecta si el cliente sigue en el mismo contexto o cambió de tema.
        # Busca CUALQUIER entidad del catálogo (producto, modelo, color, etc.),
        # no solo producto/modelo.
        # 
        # ✅ AHORA: Guarda las entidades detectadas en el estado
        # ============================================
        historial_texto = task_data.get('historial_texto', '')
        if not historial_texto:
            logger.warning("⚠️ No hay historial_texto en task_data, usando vacío")
        else:
            logger.info(f"📋 Historial recibido: {len(historial_texto)} caracteres")

        logger.info("🔍 [Puerta 2.5] Verificando contexto vs catálogo...")
        resolver = EntityResolver()

        entidades_en_mensaje = resolver.extract_entities(message)

        cambio_de_contexto = False
        entidad_conflicto = None

        # ============================================
        # ✅ NUEVO: Guardar entidades detectadas
        # ============================================
        entidades_a_guardar = {}

        for ent in entidades_en_mensaje:
            entidad_nombre = ent['entidad_nombre']
            termino_detectado = ent['termino']
            valor_en_state = state.get(entidad_nombre)
            
            # Guardar la entidad para actualizar el estado
            entidades_a_guardar[entidad_nombre] = termino_detectado

            if valor_en_state and str(valor_en_state).lower() != str(termino_detectado).lower():
                cambio_de_contexto = True
                entidad_conflicto = entidad_nombre
                logger.info(
                    f"🔄 [Puerta 2.5] Cambio de contexto en '{entidad_nombre}': "
                    f"'{valor_en_state}' → '{termino_detectado}'"
                )
            elif valor_en_state:
                logger.info(f"✅ [Puerta 2.5] '{entidad_nombre}' idéntico: '{valor_en_state}'")
            else:
                logger.info(f"🆕 [Puerta 2.5] '{entidad_nombre}'='{termino_detectado}' no estaba en state")

        # ============================================
        # ✅ NUEVO: Actualizar estado con entidades detectadas
        # ============================================
        if entidades_a_guardar:
            logger.info(f"💾 [Puerta 2.5] Guardando entidades en estado: {entidades_a_guardar}")
            state_manager.update_state(contact_id, entidades_a_guardar)
            # Recargar el estado actualizado
            state = state_manager.get_state(contact_id)
            logger.info(f"📦 [Puerta 2.5] Estado actualizado: producto='{state.get('producto')}', modelo='{state.get('modelo')}'")

        if not entidades_en_mensaje:
            logger.info("🤷 [Puerta 2.5] Sin entidades detectadas en el mensaje")

        if cambio_de_contexto:
            logger.info(f"⏭️ [Puerta 2.5] Resultado: CAMBIO DE CONTEXTO ('{entidad_conflicto}')")
        else:
            logger.info("➡️ [Puerta 2.5] Resultado: MISMO CONTEXTO (o sin entidades)")

        # ============================================
        # 2.6 GATE: ¿ES ACLARACIÓN DE LA ÚLTIMA BÚSQUEDA?
        # Solo se evalúa si la Puerta 2.5 NO detectó cambio de contexto.
        # Si el LLM confirma que sí, se reusa producto+modelo ya resuelto
        # y se va DIRECTO a resolver_y_responder_catalogo (que ya usa la
        # caché de búsquedas), sin pasar por tool-calling ni EntityResolver.
        # ============================================
        if cambio_de_contexto:
            logger.info("⏭️ [Puerta 2.6] Saltada: cambio de contexto detectado en 2.5 → ruta normal del LLM")
        elif es_aclaracion_de_busqueda(message, historial_texto, state):
            logger.info("🔁 Mensaje tratado como ACLARACIÓN, reusando búsqueda previa")

            state_manager.update_state(contact_id, {
                'producto': state.get('ultimo_producto_consultado'),
                'modelo': state.get('ultimo_modelo_consultado'),
            })
            state = state_manager.get_state(contact_id)

            resultado_aclaracion = resolver_y_responder_catalogo(
                state=state,
                contact_id=contact_id,
                intencion=state.get('ultima_intencion', 'intencion_compra'),
                channel=channel,
                historial_texto=historial_texto,
                mensaje_actual=message,
            )

            if resultado_aclaracion is not None:
                return resultado_aclaracion
            logger.warning("⚠️ resolver_y_responder_catalogo devolvió None en aclaración, cayendo a flujo normal")


        # ============================================
        # 3. INICIALIZAR CLIENTE Y CACHE
        # ============================================
        catalog_cache = CatalogCache()
        client = OpenAI(
            base_url="https://openrouter.ai/api/v1",
            api_key=os.getenv("OPENROUTER_API_KEY"),
        )

        # ============================================
        # 4. LLM: TOOL CALLING (Clasifica + Extrae entidades)
        # ============================================
        herramientas = catalog_cache.get_herramientas()

        # ✅ Construir texto con nombres Y descripciones
        herramientas_texto = ""
        for t in herramientas:
            nombre = t['function']['name']
            descripcion = t['function'].get('description', '')
            herramientas_texto += f"- {nombre}: {descripcion}\n"

        logger.info(f"📋 Historial para tool calling: {len(historial_texto)} caracteres")
        if historial_texto:
            logger.info(f"📋 Primeros 200 caracteres del historial: {historial_texto[:200]}...")

        system_prompt = load_prompt(
            "prompt_seleccion_herramienta",
            nombre_cliente=first_name,
            producto=state.get('producto', 'no especificado'),
            modelo=state.get('modelo', 'no especificado'),
            intencion=state.get('ultima_intencion', 'ninguna'),
            historial_texto=historial_texto,
            mensaje=message,
            herramientas_disponibles=herramientas_texto,  # ✅ Con descripciones
        )

        # 🔥 LOG DEL PROMPT COMPLETO
        logger.info("=" * 80)
        logger.info("📝 PROMPT SELECCIÓN HERRAMIENTA (COMPLETO):")
        logger.info("=" * 80)
        logger.info(system_prompt)
        logger.info("=" * 80)
        logger.info(f"📝 MENSAJE USUARIO: {message}")
        logger.info(f"📝 TOOLS DISPONIBLES: {[t['function']['name'] for t in herramientas]}")
        logger.info("=" * 80)

        tool_response = client.chat.completions.create(
            model="openai/gpt-4o-mini",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": message},
            ],
            tools=herramientas,
            tool_choice="required",
            temperature=0.1,
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
            razon = f"Tool seleccionada por el modelo: {intencion}"
        else:
            intencion = INTENCION_FALLBACK
            entidades_detectadas = {}
            razon = "El modelo no seleccionó ninguna herramienta"

        logger.info(f"🎯 Herramienta seleccionada: {intencion}")
        logger.info(f"🔍 Entidades LLM: {entidades_detectadas}")
        logger.info(f"   Razón: {razon}")

        # ============================================
        # 5. RESOLVER: BÚSQUEDA SEMÁNTICA CON ENTIDADES DEL LLM
        # NUEVO: EntityResolver SOLO busca en catálogo
        # ============================================
        resolver = EntityResolver()
        
        # Usar el nuevo método que busca con entidades del LLM
        resultado_resolucion = resolver.resolver_con_entidades_llm(
            contact_id=contact_id,
            entidades_llm=entidades_detectadas
        )

        # Actualizar state con lo que se encontró
        if resultado_resolucion['resolved']:
            state_manager.update_state(contact_id, resultado_resolucion['resolved'])
            state = state_manager.get_state(contact_id)
            logger.info(f"🔍 Entidades resueltas: {resultado_resolucion['resolved']}")
        else:
            logger.info("🔍 Entidades resueltas: Ninguna")

        # Guardar flags de resolución en state
        state_manager.update_state(contact_id, {
            'product_found': resultado_resolucion['product_found'],
            'model_found': resultado_resolucion['model_found'],
            'estado_resolucion': resultado_resolucion['estado'],
            'ultima_intencion': intencion,
            'updated_at': datetime.now().isoformat()
        })
              
        # ============================================
        # 6. PERSISTENCIA DE INTENCIÓN
        # ============================================
        intencion_anterior = state.get('ultima_intencion')
        if intencion_anterior and intencion_anterior != intencion:
            logger.info(f"🔄 CAMBIO DE INTENCIÓN: '{intencion_anterior}' → '{intencion}'")
            state_manager.update_state(contact_id, {'ultima_intencion': intencion})
            state = state_manager.get_state(contact_id)
            logger.info(f"📦 Contexto: producto='{state.get('producto')}', modelo='{state.get('modelo')}'")

        # ============================================
        # 7. DESPACHO AL MANEJADOR CON INYECCIÓN DE PROMPTS (UNIFICADO)
        # ============================================
        # Este flujo maneja TODOS los handlers:
        # 1. Primero verifica si el handler quiere inyección de prompt (tool_output)
        # 2. Si no, usa el flujo tradicional
        
        ctx = IntentContext(
            message=message,
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
            historial_texto=historial_texto,
            resolution=resultado_resolucion,
        )

        # Buscar el manejador para la intención
        manejador = obtener_manejador(intencion)
        
        if manejador:
            resultado_manejador = manejador(ctx)
            
            # ============================================================
            # FLUJO DE INYECCIÓN (si el handler retornó tool_output)
            # ============================================================
            if resultado_manejador and resultado_manejador.get('tool_output'):
                logger.info(f"🔄 Inyección de prompt detectada para: {intencion}")
                
                try:
                    from app.intenciones.inyection_prompt_from_tool import inyectar_y_generar
                    from app.ghl import send_message_to_ghl
                    
                    # Generar respuesta natural
                    respuesta = inyectar_y_generar(
                        tool_output=resultado_manejador['tool_output'],
                        user_message=message,
                        first_name=first_name,
                        history=historial_texto,
                        client=client
                    )
                    
                    # Enviar mensaje
                    logger.info(f"📤 Enviando respuesta generada: {respuesta[:100]}...")
                    send_message_to_ghl(contact_id, respuesta, channel)
                    
                    # Actualizar estado
                    state_manager.update_state(contact_id, {
                        'ultima_respuesta': respuesta,
                        f'ultima_respuesta_{intencion}': datetime.now().isoformat(),
                        'tool_output_usado': True,
                        'respuesta_generada_por_llm': True,
                    })
                    
                    # Limpiar flags de resolución
                    state_manager.update_state(contact_id, {
                        'product_found': False,
                        'model_found': False,
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
                        "processed_at": datetime.now().isoformat()
                    }
                    
                except Exception as e:
                    logger.error(f"❌ Error en flujo de inyección: {e}")
                    import traceback
                    logger.error(traceback.format_exc())
                    # Si falla la inyección, usar el resultado tradicional del manejador
                    if resultado_manejador is not None:
                        logger.info("ℹ️ Fallback al resultado tradicional del manejador")
                        state_manager.update_state(contact_id, {
                            'product_found': False,
                            'model_found': False,
                            'esperando_confirmacion': False,
                            'esperando_respuesta': False,
                        })
                        return resultado_manejador
            
            # ============================================================
            # FLUJO TRADICIONAL (si el handler NO retornó tool_output)
            # ============================================================
            if resultado_manejador is not None:
                logger.info(f"ℹ️ Flujo tradicional para: {intencion}")
                state_manager.update_state(contact_id, {
                    'product_found': False,
                    'model_found': False,
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

        # Limpiar flags después del fallback
        state_manager.update_state(contact_id, {
            'product_found': False,
            'model_found': False,
            'esperando_confirmacion': False,
            'esperando_respuesta': False,
        })

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
            logger.info(f"📊 Estado resolución: {estado_actual.get('estado_resolucion', 'N/A')}")
        logger.info("=" * 60)

        logger.info("✅ Worker completado")

        return resultado_final

    except Exception as e:
        logger.error(f"❌ Error: {str(e)}")
        import traceback
        logger.error(traceback.format_exc())
        raise

def generate_response_from_tool_output(tool_output, user_message, contact_id, first_name, history):
    """
    Genera una respuesta natural usando el tool_output como contexto
    """
    from app.llm.client import get_llm_client
    
    client = get_llm_client()
    
    # Construir mensajes para el LLM
    messages = [
        {"role": "system", "content": tool_output}
    ]
    
    # Agregar historial (últimos 5 mensajes)
    if history:
        history_text = "\n".join([
            f"Cliente: {h.get('user', '')}" if h.get('user') else f"Asistente: {h.get('assistant', '')}"
            for h in history[-5:]
            if h.get('user') or h.get('assistant')
        ])
        messages.append({
            "role": "user", 
            "content": f"Historial reciente:\n{history_text}\n\nMensaje actual de {first_name or 'Cliente'}: {user_message}"
        })
    else:
        messages.append({
            "role": "user",
            "content": f"Mensaje de {first_name or 'Cliente'}: {user_message}"
        })
    
    # Llamar al LLM para generar respuesta natural
    try:
        response = client.chat.completions.create(
            model="anthropic/claude-3.5-sonnet",
            messages=messages,
            temperature=0.7,
            max_tokens=500
        )
        return response.choices[0].message.content
    except Exception as e:
        logger.error(f"❌ Error generando respuesta natural: {e}")
        # Fallback: respuesta simple
        return f"Hola {first_name or 'cliente'}, para compras al por mayor puedes descargar nuestros listados en https://quinchau.com/downloader y revisar las ofertas en https://quinchau.com/ofertas. ¿Necesitas ayuda con algún producto en particular?"

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