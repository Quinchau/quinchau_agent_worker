"""
Intención: consulta_ubicacion_horario
"""
import logging
from datetime import datetime

from ..ghl import send_message_to_ghl
from ..prompts import load_prompt
from .context import IntentContext, registrar

logger = logging.getLogger(__name__)


@registrar("consulta_ubicacion_horario")
def handle(ctx: IntentContext) -> dict:
    logger.info("📍 Procesando consulta de ubicación/horario")

    prompt_ubicacion = load_prompt(
        "prompt_consulta_ubicacion_horario",
        first_name=ctx.first_name,
        message=ctx.message,
    )

    respuesta = ctx.client.chat.completions.create(
        model="openai/gpt-4o-mini",
        messages=[{"role": "user", "content": prompt_ubicacion}],
        temperature=0.5,
        max_tokens=150,
    ).choices[0].message.content.strip()

    ctx.state_manager.update_state(ctx.contact_id, {
        'ubicacion': None,
        'ultima_intencion': ctx.intencion,
    })

    send_message_to_ghl(ctx.contact_id, respuesta, ctx.channel)
    logger.info(f"📤 Respuesta de ubicación: {respuesta}")

    return {
        "success": True,
        "response": respuesta,
        "contact_id": ctx.contact_id,
        "intencion": ctx.intencion,
        "processed_at": datetime.now().isoformat(),
    }