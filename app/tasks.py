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

INTENCIONES_CON_CATALOGO = ['intencion_compra_repuestos', 'consulta_disponibilidad', 'intencion_compra', 'consulta_precio']
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


def send_multiple_messages(contact_id, messages, channel, delay=0.5):
    """
    Envía múltiples mensajes consecutivos a GHL.
    """
    import time

    for i, msg in enumerate(messages):
        if not msg or not msg.strip():
            continue
        send_message_to_ghl(contact_id, msg.strip(), channel)
        logger.info(f"📤 Mensaje {i+1}/{len(messages)} enviado: {msg[:50]}...")
        if i < len(messages) - 1:
            time.sleep(delay)


def resolver_y_responder_catalogo(state, contact_id, intencion, channel):
    """
    Resuelve producto+modelo y DELEGA al LLM la selección del producto correcto.
    🔥 SOLO ENVÍA LA URL - NADA DE TEXTO ADICIONAL

    ✅ DESPUÉS DE RESOLVER:
    - Limpia el producto (ya se consultó)
    - Mantiene el modelo para contexto (el usuario puede seguir preguntando)
    """
    try:
        producto = state.get('producto')
        modelo = state.get('modelo')

        if not producto or not modelo:
            logger.warning(f"⚠️ Faltan producto o modelo para {contact_id}")
            return None

        logger.info(f"🔍 Resolviendo catálogo: producto='{producto}', modelo='{modelo}'")

        # Consultar backend
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

        resultados = data.get('results', [])

        if not resultados:
            respuesta = f"No encontré '{producto}' para {modelo}. ¿Podrías verificar el nombre del producto?"
            send_message_to_ghl(contact_id, respuesta, channel)

            state_manager = AgentStateManager()
            state_manager.update_state(contact_id, {
                'producto': None,
                'entidades_no_resueltas': [],
                'ultimo_producto_consultado': producto,
                'ultimo_modelo_consultado': modelo
            })
            logger.info(f"🧹 Producto limpiado (no encontrado), modelo mantenido para contexto")

            return {"success": True, "response": respuesta}

        # ============================================
        # CASO 1: UN SOLO PRODUCTO - URL DIRECTA
        # ============================================
        if len(resultados) == 1:
            url = resultados[0].get('url', '')
            respuesta = url

            send_message_to_ghl(contact_id, respuesta, channel)

            state_manager = AgentStateManager()
            state_manager.update_state(contact_id, {
                'producto': None,
                'entidades_no_resueltas': [],
                'ultimo_producto_consultado': producto,
                'ultimo_modelo_consultado': modelo
            })
            logger.info(f"🧹 Producto limpiado (resuelto), modelo mantenido para contexto")

            return {
                "success": True,
                "response": respuesta,
                "contact_id": contact_id,
                "intencion": intencion,
                "producto_resuelto": producto,
                "modelo_contexto": modelo,
                "total_resultados": 1,
                "processed_at": datetime.now().isoformat()
            }

        # ============================================
        # CASO 2: MÚLTIPLES PRODUCTOS - LLM FILTRA
        # ============================================

        productos_texto = ""
        for i, item in enumerate(resultados[:10], 1):
            productos_texto += f"{i}. {item.get('description')} (Código: {item.get('stockid')}) - Stock: {item.get('stock', 0)} unidades\n"
            productos_texto += f"   URL: {item.get('url')}\n\n"

        historial_texto = ""
        turnos = state.get('ultimos_turnos', [])[-3:]
        if turnos:
            historial_texto = "Historial reciente:\n"
            for t in turnos:
                historial_texto += f"Cliente: {t['cliente']}\n"
                historial_texto += f"Asistente: {t['asistente']}\n\n"

        prompt_seleccion = f"""
        Eres Quinchau Assistant, asistente conversacional.
        El cliente se llama {state.get('nombre_cliente', 'Cliente')}.

        CONTEXTO:
        - Producto buscado: {producto}
        - Modelo: {modelo}
        - Intención: {intencion}

        {historial_texto}

        PRODUCTOS ENCONTRADOS:
        {productos_texto}

        INSTRUCCIONES:
        1. Analiza el contexto de la conversación.
        2. Elige el producto que MEJOR responda a la pregunta del cliente.
        3. ⚠️ PRIORIZA productos con STOCK DISPONIBLE (stock > 0).
        4. Si el cliente pidió algo específico (ej: "izquierdo", "derecho"), elige ese.
        5. Si hay múltiples opciones con stock, elige la que tenga mayor stock.
        6. Si ningún producto tiene stock, elige el más relevante.
        7. Responde con un JSON con el producto seleccionado.

        RESPUESTA EN JSON:
        {{
            "seleccionado": true/false,
            "stockid": "259-897",
            "description": "Mando Izquierdo HJ110",
            "url": "https://quinchau.com/producto/259-897/mando-izquierdo-hj110",
            "stock": 5,
            "razon": "El cliente preguntó por 'el izquierdo', y este es el mando izquierdo con stock disponible"
        }}

        Si ningún producto es relevante, responde con seleccionado=false.
        """

        llm_client = OpenAI(
            base_url="https://openrouter.ai/api/v1",
            api_key=os.getenv("OPENROUTER_API_KEY"),
        )

        seleccion_response = llm_client.chat.completions.create(
            model="openai/gpt-4o-mini",
            messages=[{"role": "user", "content": prompt_seleccion}],
            temperature=0.2,
            response_format={"type": "json_object"}
        )

        seleccion = json.loads(seleccion_response.choices[0].message.content)

        if seleccion.get('seleccionado', False):
            url = seleccion.get('url', '')
            stockid = seleccion.get('stockid', '')
            description = seleccion.get('description', '')
            stock = seleccion.get('stock', 0)
            razon = seleccion.get('razon', '')

            respuesta = url

            logger.info(f"🧠 LLM seleccionó: {description} (Código: {stockid})")
            logger.info(f"   Stock: {stock}")
            logger.info(f"   Razón: {razon}")

        else:
            logger.warning(f"⚠️ LLM no seleccionó, usando el primer producto de {len(resultados)} resultados")

            primer_producto = resultados[0]
            url = primer_producto.get('url', '')
            stockid = primer_producto.get('stockid', '')
            description = primer_producto.get('description', '')
            stock = primer_producto.get('stock', 0)

            respuesta = url

            logger.info(f"📦 Fallback: usando {description} (Código: {stockid}) - Stock: {stock}")

        send_message_to_ghl(contact_id, respuesta, channel)

        state_manager = AgentStateManager()
        state_manager.update_state(contact_id, {
            'producto': None,
            'entidades_no_resueltas': [],
            'ultimo_producto_consultado': producto,
            'ultimo_modelo_consultado': modelo
        })
        logger.info(f"🧹 Producto limpiado (resuelto), modelo '{modelo}' mantenido para contexto")

        return {
            "success": True,
            "response": respuesta,
            "contact_id": contact_id,
            "intencion": intencion,
            "seleccionado": seleccion.get('seleccionado', False),
            "producto_seleccionado": seleccion.get('stockid') if seleccion.get('seleccionado') else stockid,
            "producto_resuelto": producto,
            "modelo_contexto": modelo,
            "total_resultados": len(resultados),
            "processed_at": datetime.now().isoformat()
        }

    except Exception as e:
        logger.error(f"❌ Error en resolución de catálogo: {str(e)}")
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

            if state.get('status_conversacion') == 'paused':
                logger.info(f"⏸️ Conversación en pausa para {contact_id}. Mensaje ignorado.")
                return {
                    "success": True,
                    "ignored": True,
                    "contact_id": contact_id,
                    "status": "paused",
                    "reason": "conversation_paused",
                    "processed_at": datetime.now().isoformat()
                }

        # ============================================
        # 3. RESOLVER ENTIDADES DEL MENSAJE
        # ============================================
        resolver = EntityResolver()
        resolution = resolver.resolve_entities(message, contact_id)

        if resolution['resolved']:
            state_manager.update_state(contact_id, resolution['resolved'])
            state = state_manager.get_state(contact_id)

        if resolution['resolved']:
            logger.info(f"🔍 Entidades resueltas: {resolution['resolved']}")
        else:
            logger.info(f"🔍 Entidades resueltas: Ninguna")

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

        historial_texto = ""
        turnos = state.get('ultimos_turnos', [])
        if turnos and len(turnos) > 0:
            historial_texto = "\nHISTORIAL RECIENTE:\n"
            for turno in turnos[-3:]:
                cliente = turno.get('cliente', '')
                asistente = turno.get('asistente', '')
                if cliente or asistente:
                    historial_texto += f"Cliente: {cliente}\n"
                    historial_texto += f"Asistente: {asistente}\n\n"

        intent_prompt = f"""
        Eres un clasificador de intenciones para una tienda de motos llamada Quinchau Motos.

        {historial_texto}

        CONTEXTO DE LA CONVERSACIÓN:
        {contexto_estado if contexto_estado else "No hay contexto previo."}

        MENSAJE DEL CLIENTE: "{message}"

        INTENCIONES POSIBLES:
        {intenciones_texto}

        INSTRUCCIONES:
        Clasifica la intención del cliente basándote en el mensaje actual y el contexto de la conversación.

        Considera lo siguiente:
        - Un saludo sin más información es solo un saludo.
        - Un saludo que introduce un tema comercial tiene intención comercial.
        - El contexto de la conversación es importante, pero no debe sobreescribir un mensaje claramente diferente.
        - Usa tu juicio para interpretar lo que el cliente realmente quiere.

        Responde SOLO con un JSON válido.

        RESPUESTA EN JSON:
        {{
            "intencion": "nombre_de_la_intencion",
            "confianza": 0.95,
            "entidades_detectadas": {{}},
            "razon": "explicación breve de por qué elegiste esta intención"
        }}
        """

        client = OpenAI(
            base_url="https://openrouter.ai/api/v1",
            api_key=os.getenv("OPENROUTER_API_KEY"),
        )

        intent_response = client.chat.completions.create(
            model="openai/gpt-4o-mini",
            messages=[{"role": "user", "content": intent_prompt}],
            temperature=0.1,
            max_tokens=150,
            response_format={"type": "json_object"}
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
        # 5. EJECUTAR ACCIÓN SEGÚN INTENCIÓN
        # Cadena if/elif unificada — cada rama tiene return propio.
        # El LLM genérico (paso 6) solo se alcanza si ninguna rama coincide.
        # ============================================

        if intencion == 'sin_clasificar':
            logger.info(f"🤔 Intención sin clasificar → activando pausa")

            nombre = state.get('nombre_cliente', 'Cliente')
            mensaje_pausa = f"Dame un minuto, {nombre}. Haré consultas respecto a tu solicitud para poder ayudarte mejor."
            send_message_to_ghl(contact_id, mensaje_pausa, channel)

            state_manager.update_state(contact_id, {
                'ultima_intencion': 'sin_clasificar',
                'status_conversacion': 'paused',
                'entidades_no_resueltas': []
            })

            logger.info(f"⏸️ Conversación en pausa para {contact_id}")

            return {
                "success": True,
                "response": mensaje_pausa,
                "contact_id": contact_id,
                "intencion": 'sin_clasificar',
                "status": "paused",
                "processed_at": datetime.now().isoformat()
            }

        elif intencion == 'intencion_cotizar_envio':
            logger.info(f"📦 Cotización de envío detectada")

            nombre = state.get('nombre_cliente', 'Cliente')

            import re
            match = re.search(r'a\s+([A-Za-záéíóúñ\s]+)', message, re.IGNORECASE)
            ciudad = match.group(1).strip() if match else "tu ubicación"

            prompt_cotizar = f"""
            Eres Quinchau Assistant, asistente de ventas.
            El cliente {nombre} pregunta cuánto cuesta el envío a {ciudad}.

            INFORMACIÓN DE ENVÍOS:
            - Despachamos a todo el país por ZOOM
            - El costo de envío varía según la ubicación y el peso
            - Para Maracay: envío el mismo día (si se confirma antes de las 2 PM)
            - El costo aproximado es de $3-$5 para envíos locales

            INSTRUCCIONES:
            - Responde de forma amable y breve (máximo 30 palabras).
            - Si la ubicación es Maracay, menciona el envío el mismo día.
            - Pregunta si necesita cotización exacta con el producto.
            """

            respuesta = client.chat.completions.create(
                model="openai/gpt-4o-mini",
                messages=[{"role": "user", "content": prompt_cotizar}],
                temperature=0.5,
                max_tokens=80,
            ).choices[0].message.content.strip()

            send_message_to_ghl(contact_id, respuesta, channel)

            state_manager.update_state(contact_id, {
                'ultima_intencion': intencion,
                'ubicacion': ciudad
            })

            logger.info(f"📤 Cotización de envío a {ciudad} enviada")

            return {
                "success": True,
                "response": respuesta,
                "contact_id": contact_id,
                "intencion": intencion,
                "ubicacion": ciudad,
                "processed_at": datetime.now().isoformat()
            }

        elif intencion == 'intencion_retiro_y_pago_personal':
            logger.info(f"🏪 Retiro y pago personal detectado")

            nombre = state.get('nombre_cliente', 'Cliente')

            prompt_retiro = f"""
            Eres Quinchau Assistant, asistente de ventas.
            El cliente {nombre} pregunta si puede retirar y pagar personalmente.

            INFORMACIÓN DE RETIRO:
            - Puede retirar y pagar personalmente en El Limón, Maracay
            - Dirección: Panadería Marin Pan, frente a Banesco
            - Horario: Lunes a Sábado de 8:00 AM a 6:00 PM

            Responde de forma amable, breve y personalizada (máximo 30 palabras).
            Confirma la dirección y horario.
            """

            respuesta = client.chat.completions.create(
                model="openai/gpt-4o-mini",
                messages=[{"role": "user", "content": prompt_retiro}],
                temperature=0.5,
                max_tokens=80,
            ).choices[0].message.content.strip()

            send_message_to_ghl(contact_id, respuesta, channel)

            state_manager.update_state(contact_id, {'ultima_intencion': intencion})

            return {
                "success": True,
                "response": respuesta,
                "contact_id": contact_id,
                "intencion": intencion,
                "processed_at": datetime.now().isoformat()
            }

        elif intencion == 'intencion_envio_por_delivery':
            logger.info(f"📦 Envío por delivery detectado")

            ciudad = state.get('ubicacion', 'tu ciudad')
            nombre = state.get('nombre_cliente', 'Cliente')

            system_prompt = f"""
            Eres Quinchau Assistant, un asistente de ventas.
            El cliente {nombre} pregunta si hacen delivery.

            📦 INFORMACIÓN DE ENVÍOS (DEBES USAR ESTA INFORMACIÓN EXACTA):
            - Despachamos a todo el país por ZOOM
            - Entregas Gratis en El Limón, Maracay
            - Delivery solo a la ciudad de Maracay - centro, Costo $5
            - Para otras ubicaciones, el costo varía según la distancia

            INSTRUCCIONES:
            - Responde de forma amable, breve y personalizada.
            - MENCIONA OBLIGATORIAMENTE la información de envíos.
            - Si el cliente está en Maracay, menciona el costo de $5.
            - Pregunta si necesita más detalles sobre el envío.
            """

            respuesta = client.chat.completions.create(
                model="openai/gpt-4o-mini",
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": f"Cliente pregunta: {message}"}
                ],
                temperature=0.3,
                max_tokens=150,
            ).choices[0].message.content.strip()

            send_message_to_ghl(contact_id, respuesta, channel)

            state_manager.update_state(contact_id, {
                'ultima_intencion': intencion,
                'ubicacion': ciudad
            })

            return {
                "success": True,
                "response": respuesta,
                "contact_id": contact_id,
                "intencion": intencion,
                "processed_at": datetime.now().isoformat()
            }

        elif intencion == 'intencion_saludo':
            logger.info(f"👋 Saludo detectado")

            system_prompt = f"""
            Eres Quinchau Assistant, un asistente amable.
            Responde con un saludo breve y cálido (máximo 15 palabras),
            preguntando cómo puedes ayudarlo.
            """

            respuesta = client.chat.completions.create(
                model="openai/gpt-4o-mini",
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": f"El cliente {first_name} dice: \"{message}\""}
                ],
                temperature=0.5,
                max_tokens=60,
            ).choices[0].message.content.strip()

            send_message_to_ghl(contact_id, respuesta, channel)

            state_manager.update_state(contact_id, {'ultima_intencion': intencion})
            state_manager.add_turno(contact_id, message, respuesta)

            return {
                "success": True,
                "response": respuesta,
                "contact_id": contact_id,
                "intencion": intencion,
                "processed_at": datetime.now().isoformat()
            }

        elif intencion == 'intencion_compra_al_mayoreo':
            logger.info(f"📦 Compra al mayoreo detectada → respuesta directa")

            mensajes = [
                "📋 Para compras de mayor puede descargar nuestros listados y hacer su pedido en excel",
                "https://quinchau.com/downloader",
                "🔥Aqui encuentras el listado de ofertas",
                "https://quinchau.com/ofertas"
            ]

            send_multiple_messages(contact_id, mensajes, channel, delay=1.0)

            state_manager.update_state(contact_id, {
                'ultima_intencion': intencion,
                'entidades_no_resueltas': []
            })

            return {
                "success": True,
                "response": mensajes,
                "contact_id": contact_id,
                "intencion": intencion,
                "processed_at": datetime.now().isoformat()
            }

        elif intencion == 'consulta_ubicacion_horario':
            logger.info(f"📍 Procesando consulta de ubicación/horario")

            prompt_ubicacion = f"""
            Eres Quinchau Assistant, asistente conversacional amable.
            El cliente se llama {first_name}.

            📍 INFORMACIÓN DE LA TIENDA:
            - QuinChau Motos comercializa todos sus productos On Line
            - No somos tienda Fisica, despachamos a todo el país por ZOOM
            - Puede retirar y pagar personalmente su pedido en el Limon, Maracay
            - Panadería Marin Pan, frente a Banesco
            - Horario: Lunes a Sábado de 8:00 AM a 6:00 PM
            - Domingo: Cerrado
            - Teléfono: +5841244307657

            PREGUNTA DEL CLIENTE: "{message}"

            Responde de forma natural, amable y completa.
            Se breve y puntual en tu respuesta.
            """

            respuesta = client.chat.completions.create(
                model="openai/gpt-4o-mini",
                messages=[{"role": "user", "content": prompt_ubicacion}],
                temperature=0.5,
                max_tokens=150,
            ).choices[0].message.content.strip()

            state_manager.update_state(contact_id, {
                'ubicacion': None,
                'ultima_intencion': intencion
            })

            send_message_to_ghl(contact_id, respuesta, channel)
            logger.info(f"📤 Respuesta de ubicación: {respuesta}")

            return {
                "success": True,
                "response": respuesta,
                "contact_id": contact_id,
                "intencion": intencion,
                "processed_at": datetime.now().isoformat()
            }

        elif intencion == 'orden_sin_despacho':
            logger.info(f"📦 Procesando consulta de retiro de pedido")

            prompt_retiro = f"""
            Eres Quinchau Assistant, asistente conversacional amable.
            El cliente se llama {first_name}.

            📦 INFORMACIÓN DE RETIRO DE PEDIDOS:
            - QuinChau Motos comercializa todos sus productos On Line
            - No somos tienda Física, despachamos a todo el país por ZOOM
            - Puede retirar y pagar personalmente su pedido en El Limón, Maracay
            - Panadería Marin Pan, frente a Banesco
            - Horario de retiro: Lunes a Sábado de 8:00 AM a 6:00 PM
            - Teléfono de contacto: +5841244307657

            PREGUNTA DEL CLIENTE: "{message}"

            INSTRUCCIONES:
            1. Responde de forma natural, no agregues informacion adicional.
            2. Sé breve y puntual en tu respuesta.
            """

            respuesta = client.chat.completions.create(
                model="openai/gpt-4o-mini",
                messages=[{"role": "user", "content": prompt_retiro}],
                temperature=0.5,
                max_tokens=150,
            ).choices[0].message.content.strip()

            send_message_to_ghl(contact_id, respuesta, channel)
            logger.info(f"📤 Respuesta de retiro: {respuesta}")

            return {
                "success": True,
                "response": respuesta,
                "contact_id": contact_id,
                "intencion": intencion,
                "processed_at": datetime.now().isoformat()
            }

        elif intencion in INTENCIONES_CON_CATALOGO:
            logger.info(f"🔍 Procesando consulta de catálogo para: {intencion}")

            intentos_resolucion = state.get('intentos_resolucion', 0)

            faltantes = intent_classifier.validate_entities(intencion, state)

            if faltantes:
                logger.info(f"⚠️ Faltan entidades: {faltantes}")

                intentos_resolucion += 1
                state_manager.update_state(contact_id, {
                    "intentos_resolucion": intentos_resolucion,
                    "entidades_no_resueltas": faltantes
                })

                if intentos_resolucion >= 3:
                    logger.warning(f"🚨 Límite de 3 intentos alcanzado. Activando fallback.")

                    modelo = state.get('modelo')
                    nombre = state.get('nombre_cliente', 'cliente')

                    if modelo:
                        url = f"https://quinchau.com/catalogo?modelo={modelo}"
                        mensaje_fallback = (
                            f"No he logrado identificar la pieza que buscas para tu {modelo}. "
                            f"Te invito a revisar el catálogo completo de tu modelo en este enlace: "
                            f"{url}. Allí podrás visualizar todos los productos disponibles."
                        )
                    else:
                        url_general = "https://quinchau.com"
                        mensaje_fallback = (
                            f"No he logrado identificar la pieza que buscas. "
                            f"Te invito a revisar nuestro catálogo general en: {url_general}"
                        )

                    state_manager.update_state(contact_id, {
                        "intentos_resolucion": 0,
                        "entidades_no_resueltas": [],
                        "fallback_activado": True,
                        "ultimo_fallback": datetime.now().isoformat()
                    })

                    send_message_to_ghl(contact_id, mensaje_fallback, channel)
                    logger.info(f"📤 Fallback enviado: {mensaje_fallback[:100]}...")

                    return {
                        "success": True,
                        "response": mensaje_fallback,
                        "contact_id": contact_id,
                        "intencion": intencion,
                        "fallback": True,
                        "modelo_contexto": modelo,
                        "processed_at": datetime.now().isoformat()
                    }

                logger.info(f"ℹ️ Intento {intentos_resolucion} de 3. Generando pregunta.")

                producto = state.get('producto')
                modelo = state.get('modelo')

                contexto_faltantes = ""
                if 'producto' in faltantes and modelo:
                    contexto_faltantes = f"El cliente tiene modelo '{modelo}' pero falta determinar el producto."
                elif 'modelo' in faltantes and producto:
                    contexto_faltantes = f"El cliente tiene producto '{producto}' pero falta determinar el modelo."
                elif 'producto' in faltantes and not modelo:
                    contexto_faltantes = "Falta determinar el producto. El cliente no tiene modelo previo."
                else:
                    contexto_faltantes = "Faltan determinar producto y modelo."

                prompt_pregunta = f"""
                Eres Quinchau Assistant, asistente conversacional amable.
                El cliente se llama {first_name} {last_name}.

                {contexto_faltantes}

                Mensaje del cliente: "{message}"
                Intención detectada: {intencion}
                Entidades faltantes: {faltantes}

                INSTRUCCIONES:
                Genera UNA SOLA PREGUNTA natural para pedir al cliente que aclare lo que falta.
                - Sé amable, específico y personalizado (usa el nombre del cliente).
                - Si el cliente mencionó un producto, pregunta por el MODELO DE LA MOTO.
                - Si el cliente mencionó un modelo, pregunta por el producto.
                - Si no mencionó nada, pregunta por ambos.
                - No incluyas saludos adicionales, solo la pregunta.
                """

                pregunta = client.chat.completions.create(
                    model="openai/gpt-4o-mini",
                    messages=[{"role": "user", "content": prompt_pregunta}],
                    temperature=0.5,
                    max_tokens=80,
                ).choices[0].message.content.strip()

                state_manager.update_state(contact_id, {
                    "entidades_no_resueltas": faltantes,
                    "esperando_confirmacion": True
                })

                send_message_to_ghl(contact_id, pregunta, channel)
                logger.info(f"📤 Pregunta generada: {pregunta}")

                return {
                    "success": True,
                    "response": pregunta,
                    "contact_id": contact_id,
                    "intencion": intencion,
                    "entidades_faltantes": faltantes,
                    "generado_por": "llm",
                    "intentos": intentos_resolucion,
                    "processed_at": datetime.now().isoformat()
                }

            # ✅ TODAS LAS ENTIDADES RESUELTAS → CONSULTAR CATÁLOGO
            logger.info(f"✅ Todas las entidades resueltas: producto={state.get('producto')}, modelo={state.get('modelo')}")

            state_manager.update_state(contact_id, {
                "intentos_resolucion": 0,
                "entidades_no_resueltas": []
            })

            catalog_result = resolver_y_responder_catalogo(state, contact_id, intencion, channel)
            if catalog_result:
                return catalog_result

            # ⚠️ Catálogo falló — caer al LLM genérico (paso 6)
            logger.warning("⚠️ Catálogo falló, usando LLM como fallback")

        else:
            # Intención desconocida no mapeada → LLM genérico
            logger.info(f"ℹ️ Intención '{intencion}' no tiene rama específica, usando LLM genérico")

        # ============================================
        # 6. LLM GENÉRICO (fallback o intenciones sin rama propia)
        # Solo se llega aquí si ninguna rama anterior hizo return,
        # o si el catálogo falló.
        # ============================================

        entidades_texto = ""
        if state.get('producto'):
            entidades_texto += f"- Producto: {state['producto']}\n"
        if state.get('modelo'):
            entidades_texto += f"- Modelo de moto: {state['modelo']}\n"
        if state.get('ultimo_modelo') and state.get('ultimo_modelo') != state.get('modelo'):
            entidades_texto += f"- Último modelo mencionado: {state['ultimo_modelo']}\n"
        if state.get('ubicacion'):
            entidades_texto += f"- Ubicación consultada: {state['ubicacion']}\n"
        if state.get('envio'):
            entidades_texto += f"- Envío consultado: {state['envio']}\n"
        if state.get('pago'):
            entidades_texto += f"- Método de pago consultado: {state['pago']}\n"

        turnos = state.get('ultimos_turnos', [])[-4:]
        historial_texto = ""
        if turnos:
            historial_texto = "Historial reciente de la conversación:\n"
            for t in turnos:
                historial_texto += f"Cliente: {t['cliente']}\n"
                historial_texto += f"Asistente: {t['asistente']}\n\n"

        system_prompt = f"""
        Eres Quinchau Assistant, un asistente conversacional amable y profesional de Quinchau Motos, una tienda de motos y repuestos.

        DATOS DEL CLIENTE:
        - Nombre: {first_name} {last_name}
        - ID: {contact_id}

        INFORMACIÓN CONOCIDA DEL CLIENTE:
        {entidades_texto if entidades_texto else "Aún no tenemos información específica del cliente en esta conversación."}

        {historial_texto if historial_texto else "No hay historial reciente de conversación."}

        REGLAS IMPORTANTES:
        1. Responde de manera AMABLE, PROFESIONAL y CONCISA.
        2. Usa el nombre del cliente para personalizar la respuesta.
        3. NO adivines productos, precios o disponibilidad que no estén confirmados.
        4. Si no tienes información suficiente, pregunta de forma natural.
        5. Mantén un tono cálido y servicial.
        """

        user_prompt = f"""
        Mensaje del cliente: {message}

        Intención detectada: {intencion}

        Responde de forma natural y útil para el cliente.
        """

        logger.info("=" * 60)
        logger.info("📝 PROMPT DEL LLM:")
        logger.info("-" * 40)
        logger.info(f"🧠 System: {system_prompt.strip()[:200]}...")
        logger.info(f"👤 User: {message}")
        logger.info("=" * 60)

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

        logger.info("=" * 60)
        logger.info("📨 RESPUESTA DEL LLM:")
        logger.info("-" * 40)
        logger.info(llm_response)
        logger.info("=" * 60)

        state_manager.add_turno(contact_id, message, llm_response)
        state_manager.update_state(contact_id, {
            "ultima_intencion": intencion,
            "ultimo_modelo": state.get('modelo')
        })

        send_message_to_ghl(contact_id, llm_response, channel)
        logger.info(f"📤 Respuesta enviada a GHL")

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