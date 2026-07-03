"""
Intención: intencion_retiro_y_pago_personal
"""
import logging
from datetime import datetime

from ..ghl import send_message_to_ghl
from ..prompts import load_prompt
from .context import IntentContext, registrar

logger = logging.getLogger(__name__)


@registrar("intencion_retiro_y_pago_personal")
def handle(ctx: IntentContext) -> dict:
    logger.info("🏪 Retiro y pago personal detectado")

    nombre = ctx.state.get('nombre_cliente', 'Cliente')

    prompt_retiro = load_prompt("prompt_intencion_retiro_y_pago_personal", nombre=nombre)

    respuesta = ctx.client.chat.completions.create(
        model="openai/gpt-4o-mini",
        messages=[{"role": "user", "content": prompt_retiro}],
        temperature=0.5,
        max_tokens=80,
    ).choices[0].message.content.strip()

    send_message_to_ghl(ctx.contact_id, respuesta, ctx.channel)
    ctx.state_manager.update_state(ctx.contact_id, {'ultima_intencion': ctx.intencion})

    return {
        "success": True,
        "response": respuesta,
        "contact_id": ctx.contact_id,
        "intencion": ctx.intencion,
        "processed_at": datetime.now().isoformat(),
    }