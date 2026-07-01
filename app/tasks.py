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

PROMPTS_DIR = os.path.join(os.path.dirname(__file__), "contextos")

# ============================================
# CONFIGURACIÓN DE CATÁLOGO
# ============================================

CATALOG_ENDPOINT = os.getenv("CATALOG_ENDPOINT", "http://backend:8000/products/internal/resolve-by-entities")
CATALOG_URL_ENDPOINT = os.getenv("CATALOG_URL_ENDPOINT", "http://quinchau-api:3003/api/agent/catalog-url") 
CATALOG_TIMEOUT = float(os.getenv("CATALOG_TIMEOUT", "3.0"))

# ============================================
# HELPER: CARGA DE PROMPTS
# ============================================

def load_prompt(name: str, **kwargs) -> str:
    """
    Carga un prompt desde contextos/<name>.txt e inyecta variables con .format().
    Los campos opcionales que no se pasen se reemplazan por cadena vacía.
    """
    path = os.path.join(PROMPTS_DIR, f"{name}.txt")
    with open(path, "r", encoding="utf-8") as f:
        template = f.read()

    class _SafeDict(dict):
        def __missing__(self, key):
            return ""

    return template.format_map(_SafeDict(**kwargs))

def get_catalog_url_for_model(modelo: str) -> dict:
    """
    Obtiene la URL del catálogo para un modelo específico usando el endpoint dedicado.
    GET /api/agent/catalog-url/:modelo
    """
    try:
        with httpx.Client(timeout=CATALOG_TIMEOUT) as client:
            # 🔥 Usar el endpoint GET dedicado
            response = client.get(
                f"{CATALOG_URL_ENDPOINT}/{modelo}"
            )
            response.raise_for_status()
            data = response.json()
            
            if data.get('success') and data.get('data'):
                catalog_data = data['data']
                return {
                    'found': True,
                    'url': catalog_data.get('url'),
                    'modelo': catalog_data.get('modelo'),
                    'idmodelo': catalog_data.get('idmodelo'),
                    'marca': catalog_data.get('marca'),
                    'modeldescrip': catalog_data.get('modeldescrip')
                }
            return None
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            logger.warning(f"⚠️ Modelo '{modelo}' no encontrado en el catálogo")
        else:
            logger.error(f"❌ Error HTTP obteniendo URL del catálogo: {e}")
        return None
    except Exception as e:
        logger.error(f"❌ Error obteniendo URL del catálogo para '{modelo}': {e}")
        return None


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
            # 🔥 FALLBACK: Usar la URL del catálogo que devuelve el backend
            catalog_url = data.get('url')  # ✅ El backend ya la proporciona
            
            if catalog_url:
                # 🔥 Dividir en 2 mensajes
                mensajes = [
                    f"No encontré '{producto}' específicamente para {modelo.upper()}. "
                    f"Te invito a revisar el catálogo completo de {modelo.upper()}:",
                    catalog_url
                ]
                logger.info(f"📦 URL del catálogo enviada: {catalog_url}")
            else:
                # Fallback absoluto si el backend no devuelve URL
                modelo_key = modelo.lower().replace(' ', '')
                mensajes = [
                    f"No encontré '{producto}' para {modelo.upper()}. "
                    f"Por favor, verifica el nombre del producto.",
                    "https://quinchau.com/repuestos-motos"
                ]
            
            # Enviar múltiples mensajes
            send_multiple_messages(contact_id, mensajes, channel, delay=0.5)

            state_manager = AgentStateManager()
            state_manager.update_state(contact_id, {
                'producto': None,
                'entidades_no_resueltas': [],
                'ultimo_producto_consultado': producto,
                'ultimo_modelo_consultado': modelo,
                'ultimo_catalogo_enviado': catalog_url if catalog_url else None
            })
            logger.info(f"🧹 Producto limpiado (no encontrado), modelo mantenido para contexto")

            return {
                "success": True, 
                "response": mensajes,
                "contact_id": contact_id,
                "intencion": intencion,
                "fallback": True,
                "catalogo_url": catalog_url if catalog_url else None,
                "processed_at": datetime.now().isoformat()
            }

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

        prompt_seleccion = load_prompt(
            "prompt_seleccion_catalogo",
            nombre_cliente=state.get('nombre_cliente', 'Cliente'),
            producto=producto,
            modelo=modelo,
            intencion=intencion,
            historial_texto=historial_texto,
            productos_texto=productos_texto,
        )

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
        if not contexto_estado:
            contexto_estado = "No hay contexto previo."

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
        # ============================================
        # 5.1 Intencion
        # ============================================
        elif intencion == 'intencion_cotizar_envio':
            logger.info(f"📦 Cotización de envío detectada")

            nombre = state.get('nombre_cliente', 'Cliente')

            import re
            match = re.search(r'a\s+([A-Za-záéíóúñ\s]+)', message, re.IGNORECASE)
            ciudad = match.group(1).strip() if match else "tu ubicación"

            prompt_cotizar = load_prompt(
                "prompt_intencion_cotizar_envio",
                nombre=nombre,
                ciudad=ciudad,
            )

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
        # ============================================
        # 5.2 Intencion
        # ============================================
        elif intencion == 'intencion_retiro_y_pago_personal':
            logger.info(f"🏪 Retiro y pago personal detectado")

            nombre = state.get('nombre_cliente', 'Cliente')

            prompt_retiro = load_prompt(
                "prompt_intencion_retiro_y_pago_personal",
                nombre=nombre,
            )

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
        # ============================================
        # 5.3 Intencion
        # ============================================
        elif intencion == 'intencion_envio_por_delivery':
            logger.info(f"📦 Envío por delivery detectado")

            ciudad = state.get('ubicacion', 'tu ciudad')
            nombre = state.get('nombre_cliente', 'Cliente')

            system_prompt = load_prompt(
                "prompt_intencion_envio_por_delivery",
                nombre=nombre,
            )

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
        # ============================================
        # 5.4 Intencion
        # ============================================
        elif intencion == 'intencion_saludo':
            logger.info(f"👋 Saludo o agradecimiento detectado - {first_name} {last_name}")
            
            system_prompt = load_prompt(
                "prompt_intencion_saludo",
                first_name=first_name,
                historial_texto=historial_texto
            )
            
            llm_response = client.chat.completions.create(
                model="openai/gpt-4o-mini",
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": f"El cliente {first_name} dice: \"{message}\""}
                ],
                temperature=0.3,
                max_tokens=60,
            )
            
            respuesta = llm_response.choices[0].message.content.strip()
            
            es_duplicado = "SALUDO_DUPLICADO" in respuesta or "{SALUDO_DUPLICADO}" in respuesta
            
            if es_duplicado:
                logger.info(f"🔄 Saludo duplicado ignorado (sin respuesta)")
                logger.debug(f"   Respuesta LLM: {respuesta}")  # Solo en debug si necesitas
                
                state_manager.add_turno(contact_id, message, "[IGNORADO - Saludo duplicado]")
                state_manager.update_state(contact_id, {
                    'ultima_intencion': intencion,
                    'saludo_duplicado_ignorado': True,
                    'ultimo_saludo_ignorado': datetime.now().isoformat()
                })
                
                return {
                    "success": True,
                    "ignored": True,
                    "contact_id": contact_id,
                    "intencion": intencion,
                    "reason": "saludo_duplicado",
                    "processed_at": datetime.now().isoformat()
                }
            
            logger.info(f"✅ Saludo enviado: {respuesta[:40]}...")
            
            send_message_to_ghl(contact_id, respuesta, channel)
            state_manager.add_turno(contact_id, message, respuesta)
            state_manager.update_state(contact_id, {'ultima_intencion': intencion})

            return {
                "success": True,
                "response": respuesta,
                "contact_id": contact_id,
                "intencion": intencion,
                "processed_at": datetime.now().isoformat()
            }
        # ============================================
        # 5.5 Intencion
        # ============================================
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
        # ============================================
        # 5.6 Intencion
        # ============================================
        elif intencion == 'consulta_ubicacion_horario':
            logger.info(f"📍 Procesando consulta de ubicación/horario")

            prompt_ubicacion = load_prompt(
                "prompt_consulta_ubicacion_horario",
                first_name=first_name,
                message=message,
            )

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
        # ============================================
        # 5.7 Intencion
        # ============================================
        elif intencion == 'orden_sin_despacho':
            logger.info(f"📦 Procesando consulta de retiro de pedido")

            prompt_retiro = load_prompt(
                "prompt_orden_sin_despacho",
                first_name=first_name,
                message=message,
            )

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
        # ============================================
        # 5.8 Intencion
        # ============================================
        elif intencion == 'intencion_compra':
            logger.info(f"🔍 Procesando consulta de catálogo (compra/disponibilidad/precio)")

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
                    producto = state.get('producto')
                    nombre = state.get('nombre_cliente', 'cliente')

                    if modelo:
                        # 🔥 Obtener URL del catálogo del modelo desde el backend
                        catalog_info = get_catalog_url_for_model(modelo)
                        
                        if catalog_info and catalog_info.get('found'):
                            catalogo_url = catalog_info.get('url')
                            modeldescrip = catalog_info.get('modeldescrip', modelo)
                            
                            if not producto or producto == 'None':
                                mensajes = [
                                    f"📋 No encuentro el producto por la definición que me das, te dejo el catálogo completo de {modeldescrip} para que intentes buscarlo tu mismo:",
                                    catalogo_url
                                ]
                            else:
                                mensajes = [
                                    f"🔍 No encontré '{producto}' específicamente para {modeldescrip}. Te invito a revisar el catálogo completo de {modeldescrip}:",
                                    catalogo_url
                                ]
                            logger.info(f"📦 URL del catálogo obtenida del backend: {catalogo_url}")
                            
                        else:
                            # ⚠️ Fallback: No se pudo obtener URL del backend
                            # Usar URL genérica como último recurso
                            catalogo_url = None
                            logger.warning(f"⚠️ No se pudo obtener URL del backend para {modelo}")
                            
                            if not producto or producto == 'None':
                                mensajes = [
                                    f"📋 No encuentro el producto por la definición que me das. Te invito a revisar el catálogo completo de {modelo}:",
                                    f"https://quinchau.com/repuestos-motos"
                                ]
                            else:
                                mensajes = [
                                    f"🔍 No encontré '{producto}' para {modelo}. Te invito a revisar el catálogo completo de {modelo}:",
                                    f"https://quinchau.com/repuestos-motos"
                                ]
                        
                        send_multiple_messages(contact_id, mensajes, channel, delay=0.5)
                        response_data = mensajes
                        mensaje_fallback = " ".join(mensajes)
                        
                    else:
                        mensaje_fallback = (
                            f"📋 No he logrado identificar la pieza que buscas. "
                            f"Te invito a revisar nuestro catálogo general en: https://quinchau.com"
                        )
                        send_message_to_ghl(contact_id, mensaje_fallback, channel)
                        response_data = mensaje_fallback

                    # 🔥 Guardar turno
                    state_manager.add_turno(contact_id, message, mensaje_fallback)

                    state_manager.update_state(contact_id, {
                        "intentos_resolucion": 0,
                        "entidades_no_resueltas": [],
                        "fallback_activado": True,
                        "ultimo_fallback": datetime.now().isoformat()
                    })

                    logger.info(f"📤 Fallback enviado para {contact_id}")

                    return {
                        "success": True,
                        "response": response_data,
                        "contact_id": contact_id,
                        "intencion": intencion,
                        "fallback": True,
                        "modelo_contexto": modelo,
                        "producto_contexto": producto,
                        "catalogo_url": catalogo_url if catalogo_url else None,
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

                prompt_pregunta = load_prompt(
                    "prompt_entidades_faltantes",
                    first_name=first_name,
                    last_name=last_name,
                    contexto_faltantes=contexto_faltantes,
                    message=message,
                    intencion=intencion,
                    faltantes=faltantes,
                )

                pregunta = client.chat.completions.create(
                    model="openai/gpt-4o-mini",
                    messages=[{"role": "user", "content": prompt_pregunta}],
                    temperature=0.5,
                    max_tokens=80,
                ).choices[0].message.content.strip()

                # 🔥 GUARDAR EL TURNO CON LA PREGUNTA
                state_manager.add_turno(contact_id, message, pregunta)

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

            # ⚠️ Catálogo falló — caer al LLM genérico
            logger.warning("⚠️ Catálogo falló, usando LLM como fallback")

        else:
            logger.info(f"ℹ️ Intención '{intencion}' no tiene rama específica, usando LLM genérico")

        # ============================================
        # 6. LLM GENÉRICO (fallback o intenciones sin rama propia)
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
        if not entidades_texto:
            entidades_texto = "Aún no tenemos información específica del cliente en esta conversación."

        turnos = state.get('ultimos_turnos', [])[-4:]
        historial_texto = ""
        if turnos:
            historial_texto = "Historial reciente de la conversación:\n"
            for t in turnos:
                historial_texto += f"Cliente: {t['cliente']}\n"
                historial_texto += f"Asistente: {t['asistente']}\n\n"
        if not historial_texto:
            historial_texto = "No hay historial reciente de conversación."

        system_prompt = load_prompt(
            "prompt_llm_generico_system",
            first_name=first_name,
            last_name=last_name,
            contact_id=contact_id,
            entidades_texto=entidades_texto,
            historial_texto=historial_texto,
        )

        user_prompt = load_prompt(
            "prompt_llm_generico_user",
            message=message,
            intencion=intencion,
        )

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