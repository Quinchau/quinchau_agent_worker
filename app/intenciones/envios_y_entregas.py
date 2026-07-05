"""
Intención: intencion_cotizar_envio
"""
import logging
import re
from datetime import datetime

from ..ghl import send_message_to_ghl
from ..prompts import load_prompt
from .context import IntentContext, registrar

logger = logging.getLogger(__name__)


@registrar("intencion_cotizar_envio")
def handle(ctx: IntentContext) -> dict:
    logger.info("📦 Cotización de envío detectada")

    nombre = ctx.state.get('nombre_cliente', 'Cliente')

    match = re.search(r'a\s+([A-Za-záéíóúñ\s]+)', ctx.message, re.IGNORECASE)
    ciudad = match.group(1).strip() if match else "tu ubicación"

    prompt_cotizar = load_prompt(
        "prompt_envios_y_entregas",
        nombre=nombre,
        ciudad=ciudad,
    )

    respuesta = ctx.client.chat.completions.create(
        model="openai/gpt-4o-mini",
        messages=[{"role": "user", "content": prompt_cotizar}],
        temperature=0.5,
        max_tokens=80,
    ).choices[0].message.content.strip()

    send_message_to_ghl(ctx.contact_id, respuesta, ctx.channel)

    ctx.state_manager.update_state(ctx.contact_id, {
        'ultima_intencion': ctx.intencion,
        'ubicacion': ciudad,
    })

    logger.info(f"📤 Cotización de envío a {ciudad} enviada")

    return {
        "success": True,
        "response": respuesta,
        "contact_id": ctx.contact_id,
        "intencion": ctx.intencion,
        "ubicacion": ciudad,
        "processed_at": datetime.now().isoformat(),
    }