"""
Intención: orden_sin_despacho
"""
import logging
from datetime import datetime

from ..ghl import send_message_to_ghl
from ..prompts import load_prompt
from .context import IntentContext, registrar

logger = logging.getLogger(__name__)


@registrar("orden_sin_despacho")
def handle(ctx: IntentContext) -> dict:
    logger.info("📦 Procesando consulta de retiro de pedido")

    prompt_retiro = load_prompt(
        "prompt_orden_sin_despacho",
        first_name=ctx.first_name,
        message=ctx.message,
    )

    respuesta = ctx.client.chat.completions.create(
        model="openai/gpt-4o-mini",
        messages=[{"role": "user", "content": prompt_retiro}],
        temperature=0.5,
        max_tokens=150,
    ).choices[0].message.content.strip()

    send_message_to_ghl(ctx.contact_id, respuesta, ctx.channel)
    logger.info(f"📤 Respuesta de retiro: {respuesta}")

    return {
        "success": True,
        "response": respuesta,
        "contact_id": ctx.contact_id,
        "intencion": ctx.intencion,
        "processed_at": datetime.now().isoformat(),
    }