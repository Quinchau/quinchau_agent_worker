import logging
from datetime import datetime
from typing import List, Optional

from ..ghl import send_message_to_ghl, send_multiple_messages
from ..intent_classifier import IntentClassifier
from ..prompts import load_prompt
from .catalogo import get_catalog_url_for_model, resolver_y_responder_catalogo
from .context import IntentContext, registrar

logger = logging.getLogger(__name__)


@registrar("intencion_compra")
def handle(ctx: IntentContext) -> Optional[dict]:
    logger.info("🔍 Procesando consulta de catálogo (compra/disponibilidad/precio)")

    intent_classifier = IntentClassifier()
    intentos_resolucion = ctx.state.get('intentos_resolucion', 0)

    faltantes = intent_classifier.validate_entities(ctx.intencion, ctx.state)

    if faltantes:
        return _manejar_entidades_faltantes(ctx, faltantes, intentos_resolucion)

    logger.info(
        f"✅ Todas las entidades resueltas: producto={ctx.state.get('producto')}, "
        f"modelo={ctx.state.get('modelo')}"
    )

    ctx.state_manager.update_state(ctx.contact_id, {
        "intentos_resolucion": 0,
        "entidades_no_resueltas": [],
        "ultima_pregunta_entidades": None,
    })

    catalog_result = resolver_y_responder_catalogo(ctx.state, ctx.contact_id, ctx.intencion, ctx.channel)
    if catalog_result:
        return catalog_result

    # ⚠️ Catálogo falló — señal para que tasks.py caiga al LLM genérico
    logger.warning("⚠️ Catálogo falló, usando LLM como fallback")
    return None


def _manejar_entidades_faltantes(ctx: IntentContext, faltantes: List[str], intentos_resolucion: int) -> dict:
    logger.info(f"⚠️ Faltan entidades: {faltantes}")

    intentos_resolucion += 1
    ctx.state_manager.update_state(ctx.contact_id, {
        "intentos_resolucion": intentos_resolucion,
        "entidades_no_resueltas": faltantes,
    })

    if intentos_resolucion >= 3:
        return _fallback_limite_intentos(ctx)

    logger.info(f"ℹ️ Intento {intentos_resolucion} de 3. Generando pregunta.")

    producto = ctx.state.get('producto')
    modelo = ctx.state.get('modelo')

    if 'producto' in faltantes and modelo:
        contexto_faltantes = f"El cliente tiene modelo '{modelo}' pero falta determinar el producto."
    elif 'modelo' in faltantes and producto:
        contexto_faltantes = f"El cliente tiene producto '{producto}' pero falta determinar el modelo."
    elif 'producto' in faltantes and not modelo:
        contexto_faltantes = "Falta determinar el producto. El cliente no tiene modelo previo."
    else:
        contexto_faltantes = "Faltan determinar producto y modelo."

    # 🔥 Le damos al LLM memoria de lo que ya preguntó, para que no se repita
    # y pueda reconocer que el mensaje actual es una respuesta a esa pregunta.
    ultima_pregunta = ctx.state.get('ultima_pregunta_entidades', '') or ''

    prompt_pregunta = load_prompt(
        "prompt_entidades_faltantes",
        first_name=ctx.first_name,
        last_name=ctx.last_name,
        contexto_faltantes=contexto_faltantes,
        message=ctx.message,
        intencion=ctx.intencion,
        faltantes=faltantes,
        intentos=intentos_resolucion,
        ultima_pregunta=ultima_pregunta,
    )

    pregunta = ctx.client.chat.completions.create(
        model="openai/gpt-4o-mini",
        messages=[{"role": "user", "content": prompt_pregunta}],
        temperature=0.5,
        max_tokens=80,
    ).choices[0].message.content.strip()

    ctx.state_manager.update_state(ctx.contact_id, {
        "entidades_no_resueltas": faltantes,
        "esperando_confirmacion": True,
        "ultima_pregunta_entidades": pregunta,
    })

    send_message_to_ghl(ctx.contact_id, pregunta, ctx.channel)
    logger.info(f"📤 Pregunta generada: {pregunta}")

    return {
        "success": True,
        "response": pregunta,
        "contact_id": ctx.contact_id,
        "intencion": ctx.intencion,
        "entidades_faltantes": faltantes,
        "generado_por": "llm",
        "intentos": intentos_resolucion,
        "processed_at": datetime.now().isoformat(),
    }


def _fallback_limite_intentos(ctx: IntentContext) -> dict:
    logger.warning("🚨 Límite de 3 intentos alcanzado. Activando fallback.")

    modelo = ctx.state.get('modelo')
    producto = ctx.state.get('producto')
    catalogo_url = None

    if modelo:
        catalog_info = get_catalog_url_for_model(modelo)

        if catalog_info and catalog_info.get('found'):
            catalogo_url = catalog_info.get('url')
            modeldescrip = catalog_info.get('modeldescrip', modelo)

            if not producto or producto == 'None':
                mensajes = [
                    f"📋 No encuentro el producto por la definición que me das, te dejo el catálogo completo "
                    f"de {modeldescrip} para que intentes buscarlo tu mismo:",
                    catalogo_url,
                ]
            else:
                mensajes = [
                    f"🔍 No encontré '{producto}' específicamente para {modeldescrip}. "
                    f"Te invito a revisar el catálogo completo de {modeldescrip}:",
                    catalogo_url,
                ]
            logger.info(f"📦 URL del catálogo obtenida del backend: {catalogo_url}")
        else:
            logger.warning(f"⚠️ No se pudo obtener URL del backend para {modelo}")

            if not producto or producto == 'None':
                mensajes = [
                    f"📋 No encuentro el producto por la definición que me das. "
                    f"Te invito a revisar el catálogo completo de {modelo}:",
                    "https://quinchau.com/repuestos-motos",
                ]
            else:
                mensajes = [
                    f"🔍 No encontré '{producto}' para {modelo}. "
                    f"Te invito a revisar el catálogo completo de {modelo}:",
                    "https://quinchau.com/repuestos-motos",
                ]

        send_multiple_messages(ctx.contact_id, mensajes, ctx.channel, delay=0.5)
        response_data = mensajes
        mensaje_fallback = " ".join(mensajes)
    else:
        mensaje_fallback = (
            "📋 No he logrado identificar la pieza que buscas. "
            "Te invito a revisar nuestro catálogo general en: https://quinchau.com"
        )
        send_message_to_ghl(ctx.contact_id, mensaje_fallback, ctx.channel)
        response_data = mensaje_fallback

    ctx.state_manager.update_state(ctx.contact_id, {
        "intentos_resolucion": 0,
        "entidades_no_resueltas": [],
        "fallback_activado": True,
        "ultimo_fallback": datetime.now().isoformat(),
        "ultima_pregunta_entidades": None,
    })

    logger.info(f"📤 Fallback enviado para {ctx.contact_id}")

    return {
        "success": True,
        "response": response_data,
        "contact_id": ctx.contact_id,
        "intencion": ctx.intencion,
        "fallback": True,
        "modelo_contexto": modelo,
        "producto_contexto": producto,
        "catalogo_url": catalogo_url,
        "processed_at": datetime.now().isoformat(),
    }