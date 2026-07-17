"""
Intención: intencion_saludo
"""
import logging
import random
from datetime import datetime

from ..ghl import send_message_to_ghl
from .context import IntentContext, registrar

logger = logging.getLogger(__name__)

SALUDOS = [
    "¡Hola {first_name}! ¿En qué puedo ayudarte hoy?",
    "Hola {first_name}, ¿cómo puedo ayudarte?",
    "¡Qué tal, {first_name}! ¿Buscas algún repuesto o tienes alguna consulta?",
]


@registrar("intencion_saludo")
def handle(ctx: IntentContext) -> dict:
    logger.info(f"👋 Saludo o agradecimiento detectado - {ctx.first_name} {ctx.last_name}")

    respuesta = random.choice(SALUDOS).format(first_name=ctx.first_name)

    logger.info(f"✅ Saludo enviado: {respuesta[:40]}...")

    send_message_to_ghl(ctx.contact_id, respuesta, ctx.channel)
    ctx.state_manager.update_state(ctx.contact_id, {'ultima_intencion': ctx.intencion})

    return {
        "success": True,
        "response": respuesta,
        "contact_id": ctx.contact_id,
        "intencion": ctx.intencion,
        "processed_at": datetime.now().isoformat(),
    }