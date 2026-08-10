# app/intenciones/solicitud_catalogo_modelo.py
"""
Handler para la intención solicitud_catalogo_modelo.

Se activa cuando el cliente pide ver el catálogo completo (sin
mencionar categoría/producto específico).

RESOLUCIÓN DE MODELO: si ctx.state.modelo está definido, se entrega el
catálogo directamente. Si es None, se delega en el LLM (leyendo mensaje
actual + historial) la interpretación de si el cliente mencionó un
modelo que no existe en nuestros registros, o simplemente no mencionó
ninguno — y si ya se le pidió aclaración antes, para decidir entre
pedir de nuevo o derivar a un humano. Sin contador de intentos.
"""
import json
import logging
import os
from datetime import datetime
from typing import Dict, Optional

from openai import OpenAI

from ..ghl import send_message_to_ghl, send_multiple_messages
from ..prompts import load_prompt
from .catalogo import get_catalog_url_for_model
from .context import IntentContext, registrar

logger = logging.getLogger(__name__)


@registrar("solicitud_catalogo_modelo")
def handle(ctx: IntentContext) -> Optional[dict]:
    logger.info("📖 Procesando solicitud de catálogo — sin filtro de producto")

    modelo = ctx.state.get('modelo')

    if modelo:
        return _responder_con_catalogo(ctx, modelo)

    decision = _decidir_aclaracion_o_derivacion_llm(ctx)
    accion = decision.get("accion")

    if accion == "derivar_humano":
        logger.info("⛔ LLM determinó que ya se pidió el modelo antes, derivando a agente humano")
        mensaje = decision.get("mensaje") or (
            "No logramos identificar el modelo de tu moto. "
            "Te voy a poner en contacto con un asesor para ayudarte directamente."
        )
        send_message_to_ghl(ctx.contact_id, mensaje, ctx.channel)
        return {
            "success": True,
            "response": mensaje,
            "contact_id": ctx.contact_id,
            "intencion": ctx.intencion,
            "fallback": True,
            "fallback_tipo": "modelo_no_resuelto_derivar_humano",
            "processed_at": datetime.now().isoformat(),
        }

    # accion == "pedir_aclaracion"
    mensaje = decision.get("mensaje") or (
        "¡Claro! ¿Cuál es el modelo de tu moto para enviarte el catálogo correspondiente?"
    )
    send_message_to_ghl(ctx.contact_id, mensaje, ctx.channel)
    return {
        "success": True,
        "response": mensaje,
        "contact_id": ctx.contact_id,
        "intencion": ctx.intencion,
        "esperando_confirmacion_modelo": True,
        "processed_at": datetime.now().isoformat(),
    }


def _responder_con_catalogo(ctx: IntentContext, modelo: str) -> dict:
    """Envía el link de catálogo del modelo ya resuelto en el estado."""
    catalog_info = get_catalog_url_for_model(modelo)

    if catalog_info and catalog_info.get('found'):
        catalogo_url = catalog_info.get('url')
        modeldescrip = catalog_info.get('modeldescrip', modelo)
        mensaje = f"Aquí tienes el catálogo completo para tu {modeldescrip} {catalogo_url}"
    else:
        catalogo_url = None
        mensaje = "Aquí tienes nuestro catálogo general:\nhttps://quinchau.com"

    send_message_to_ghl(ctx.contact_id, mensaje, ctx.channel)

    ctx.state_manager.update_state(ctx.contact_id, {
        "esperando_confirmacion": False,
        "esperando_respuesta": False,
    })

    return {
        "success": True,
        "response": mensajes,
        "contact_id": ctx.contact_id,
        "intencion": ctx.intencion,
        "modelo_contexto": modelo,
        "catalogo_url": catalogo_url,
        "processed_at": datetime.now().isoformat(),
    }


def _decidir_aclaracion_o_derivacion_llm(ctx: IntentContext) -> Dict:
    """
    Decide, vía LLM, si pedir aclaración del modelo o derivar a humano,
    leyendo mensaje actual + historial completo.
    """
    system_prompt = load_prompt(
        "prompt_solicitud_catalogo_modelo",
        nombre_cliente=ctx.first_name,
        historial_texto=ctx.historial_texto,
        mensaje_actual=ctx.message,
    )

    try:
        llm_client = OpenAI(
            base_url="https://openrouter.ai/api/v1",
            api_key=os.getenv("OPENROUTER_API_KEY"),
        )
        response = llm_client.chat.completions.create(
            model="openai/gpt-4o-mini",
            messages=[{"role": "user", "content": system_prompt}],
            temperature=0.1,
            response_format={"type": "json_object"},
        )
        data = json.loads(response.choices[0].message.content)
        logger.info(f"🎯 Resolución de modelo (catálogo) | Acción: {data.get('accion')} | Razón: {data.get('razon', '')}")
        return data

    except (json.JSONDecodeError, KeyError, IndexError) as e:
        logger.error(f"❌ Error parseando decisión de aclaración/derivación de modelo: {e}")
        return {
            "accion": "pedir_aclaracion",
            "mensaje": "No logramos identificar el modelo de tu moto. ¿Podrías confirmarme cuál es?",
            "razon": "error_parseo",
        }