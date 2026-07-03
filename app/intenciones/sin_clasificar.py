"""
Intención: sin_clasificar

El clasificador no pudo determinar qué quiere el cliente. Se avisa que se
va a investigar y se pausa la conversación hasta que se reactive.
"""
import logging
from datetime import datetime

from ..ghl import send_message_to_ghl
from .context import IntentContext, registrar

logger = logging.getLogger(__name__)


@registrar("sin_clasificar")
def handle(ctx: IntentContext) -> dict:
    logger.info("🤔 Intención sin clasificar → activando pausa")

    nombre = ctx.state.get('nombre_cliente', 'Cliente')
    mensaje_pausa = f"Dame un minuto, {nombre}. Haré consultas respecto a tu solicitud para poder ayudarte mejor."
    send_message_to_ghl(ctx.contact_id, mensaje_pausa, ctx.channel)

    ctx.state_manager.update_state(ctx.contact_id, {
        'ultima_intencion': 'sin_clasificar',
        'status_conversacion': 'paused',
        'entidades_no_resueltas': [],
    })

    logger.info(f"⏸️ Conversación en pausa para {ctx.contact_id}")

    return {
        "success": True,
        "response": mensaje_pausa,
        "contact_id": ctx.contact_id,
        "intencion": "sin_clasificar",
        "status": "paused",
        "processed_at": datetime.now().isoformat(),
    }