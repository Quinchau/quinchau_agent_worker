import os
import json
import copy
import re
import logging
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
VALOR_SIN_MATCH_PRODUCTO = "ninguno_coincide"


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


def _parchar_enum_producto(herramientas: List[Dict], productos: List[Dict], bloqueantes_map: Dict) -> List[Dict]:
    if not productos:
        return herramientas

    opciones = [p['id'] for p in productos if p.get('id')]
    opciones.append(VALOR_SIN_MATCH_PRODUCTO)

    herramientas_parcheadas = copy.deepcopy(herramientas)

    for tool in herramientas_parcheadas:
        nombre_intencion = tool['function']['name']
        entidades_bloqueantes = bloqueantes_map.get(nombre_intencion, [])

        if 'producto' not in entidades_bloqueantes:
            continue

        propiedades = tool['function']['parameters']['properties']
        if 'producto' in propiedades:
            listado_legible = "; ".join(
                f"{p['id']}: {p['nombre']}" for p in productos if p.get('id')
            )
            descripcion_base = propiedades['producto'].get('description', '')
            ejemplo_id = opciones[0] if opciones else 'ID'

            propiedades['producto'] = {
                "type": "array",
                "items": {
                    "type": "string",
                    "enum": opciones
                },
                "description": (
                    f"{descripcion_base} "
                    f"IMPORTANTE: la respuesta SIEMPRE es una lista, incluso con un solo "
                    f"resultado — ej. ['{ejemplo_id}'], nunca un string suelto sin corchetes. "
                    f"TODOS los productos de este catálogo YA SON COMPATIBLES con el modelo "
                    f"del cliente (el filtro de compatibilidad ya se hizo antes) — no descartes "
                    f"un producto solo porque su nombre no menciona el modelo. "
                    f"Tu único criterio es: ¿el NOMBRE del producto coincide con lo que pide "
                    f"el cliente? Si el cliente pide algo genérico (ej. 'bujía' sin marca ni "
                    f"tipo), devolvé TODOS los productos cuyo nombre corresponda a ese tipo de "
                    f"repuesto, sin importar cuántos sean. Si el cliente da un detalle que "
                    f"distingue un producto de los demás (marca, variante, ubicación, "
                    f"capacidad, código específico), devolvé solo el/los que matcheen ese "
                    f"detalle. Catálogo disponible: {listado_legible}. "
                    f"Si ninguno corresponde, devolvé únicamente ['{VALOR_SIN_MATCH_PRODUCTO}']."
                ),
            }

    return herramientas_parcheadas

# ============================================
# HELPERS DEL NUEVO FLUJO
# ============================================

def _parchar_enum_producto(herramientas: List[Dict], productos: List[Dict], bloqueantes_map: Dict) -> List[Dict]:
    """
    Devuelve una copia de `herramientas` donde, para toda intención cuyas
    entidades bloqueantes incluyan 'producto', la propiedad 'producto' del
    schema pasa de texto libre a un enum con los productos reales del
    modelo vigente + un valor especial para "no está en la lista".
    """
    if not productos:
        return herramientas

    opciones = [p.get('stockid') or p.get('id') for p in productos if p.get('stockid') or p.get('id')]
    opciones.append(VALOR_SIN_MATCH_PRODUCTO)

    herramientas_parcheadas = copy.deepcopy(herramientas)

    for tool in herramientas_parcheadas:
        nombre_intencion = tool['function']['name']
        entidades_bloqueantes = bloqueantes_map.get(nombre_intencion, [])

        if 'producto' not in entidades_bloqueantes:
            continue

        propiedades = tool['function']['parameters']['properties']
        if 'producto' in propiedades:
            propiedades['producto']['enum'] = opciones
            # Descripción con contexto legible para el LLM (nombre/descr real
            # de cada producto, no solo el id)
            listado_legible = "; ".join(
                f"{p.get('stockid') or p.get('id')}: {p.get('description') or p.get('nombre', '')}"
                for p in productos
            )
            propiedades['producto']['description'] = (
                f"{propiedades['producto'].get('description', '')} "
                f"Elegí uno de estos productos reales del catálogo: {listado_legible}. "
                f"Si ninguno corresponde a lo que pide el cliente, usá '{VALOR_SIN_MATCH_PRODUCTO}'."
            )

    return herramientas_parcheadas

def _normalizar_alias(texto: str, alias: Optional[str], modelo: str) -> str:
    """
    Reemplaza el alias de modelo detectado por su término canónico,
    para que el LLM vea concordancia entre el mensaje y el modelo ya
    resuelto en el state (ej. "artistic" -> "JOGS").
    """
    if not texto or not alias:
        return texto

    patron = r'\b' + re.escape(alias) + r'\b'
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

    return {
        "intencion": intencion,
        "entidades_detectadas": entidades_detectadas,
        "razon": razon,
        "tool_calls": tool_calls,
    }


# ============================================
# PROCESAMIENTO DE MENSAJES GHL
# ============================================

def process_ghl_message(task_data: Dict[str, Any]) -> Dict[str, Any]:
    """Procesa el mensaje de GHL:
    FLUJO: Gate 2.5 (modelo, solo contexto) → LLM tool-calling (una sola
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
        # 2.5 GATE — RESOLVER MODELO (solo contexto, sin catálogo)
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

        # Normaliza el alias (del mensaje actual o heredado del state) a su
        # término canónico, en TODO lo que vaya a viajar hacia un LLM —
        # mensaje actual e historial — para que ninguna llamada quede
        # expuesta al alias crudo.
        message_normalizado = _normalizar_alias(message, alias_usado, modelo_resuelto)
        historial_normalizado = _normalizar_alias(historial_texto, alias_usado, modelo_resuelto)

        # ============================================
        # 4. CLIENTE OPENAI + TOOLS BASE
        # ============================================
        client = OpenAI(
            base_url="https://openrouter.ai/api/v1",
            api_key=os.getenv("OPENROUTER_API_KEY"),
        )

        herramientas_base = catalog_cache.get_herramientas()

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
                    from app.ghl import send_message_to_ghl

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
                    if resultado_manejador is not None:
                        logger.info("ℹ️ Fallback al resultado tradicional del manejador")
                        state_manager.update_state(contact_id, {
                            'esperando_confirmacion': False,
                            'esperando_respuesta': False,
                        })
                        return resultado_manejador

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