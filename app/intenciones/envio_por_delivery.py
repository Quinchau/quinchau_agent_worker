"""
Intención: intencion_envio_por_delivery
"""
import logging
from datetime import datetime

from ..ghl import send_message_to_ghl
from ..prompts import load_prompt
from .context import IntentContext, registrar

logger = logging.getLogger(__name__)


@registrar("intencion_envio_por_delivery")
def handle(ctx: IntentContext) -> dict:
    logger.info("📦 Envío por delivery detectado")

    ciudad = ctx.state.get('ubicacion', 'tu ciudad')
    nombre = ctx.state.get('nombre_cliente', 'Cliente')

    system_prompt = load_prompt("prompt_intencion_envio_por_delivery", nombre=nombre)

    respuesta = ctx.client.chat.completions.create(
        model="openai/gpt-4o-mini",
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": f"Cliente pregunta: {ctx.message}"},
        ],
        temperature=0.3,
        max_tokens=150,
    ).choices[0].message.content.strip()

    send_message_to_ghl(ctx.contact_id, respuesta, ctx.channel)

    ctx.state_manager.update_state(ctx.contact_id, {
        'ultima_intencion': ctx.intencion,
        'ubicacion': ciudad,
    })

    return {
        "success": True,
        "response": respuesta,
        "contact_id": ctx.contact_id,
        "intencion": ctx.intencion,
        "processed_at": datetime.now().isoformat(),
    }