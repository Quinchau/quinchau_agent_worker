"""
Intención: sin_clasificar

El clasificador no pudo determinar qué quiere el cliente. Se avisa que se
va a investigar y se pausa la conversación hasta que se reactive.
"""
import logging
import os
from datetime import datetime, timedelta

from ..ghl import send_message_to_ghl
from .context import IntentContext, registrar

logger = logging.getLogger(__name__)

PAUSA_MINUTOS = int(os.getenv("PAUSA_MINUTOS", "3"))


@registrar("sin_clasificar")
def handle(ctx: IntentContext) -> dict:
    logger.info(f"🤔 Intención sin clasificar → activando pausa de {PAUSA_MINUTOS} min")

    nombre = ctx.state.get('nombre_cliente', 'Cliente')
    mensaje_pausa = f"Dame un minuto, {nombre}. Haré consultas respecto a tu solicitud para poder ayudarte mejor."
    send_message_to_ghl(ctx.contact_id, mensaje_pausa, ctx.channel)

    pausa_hasta = (datetime.now() + timedelta(minutes=PAUSA_MINUTOS)).isoformat()

    ctx.state_manager.update_state(ctx.contact_id, {
        'ultima_intencion': 'sin_clasificar',
        'status_conversacion': 'paused',
        'pausa_hasta': pausa_hasta,
        'entidades_no_resueltas': [],
    })

    logger.info(f"⏸️ Conversación en pausa para {ctx.contact_id} hasta {pausa_hasta}")

    return {
        "success": True,
        "response": mensaje_pausa,
        "contact_id": ctx.contact_id,
        "intencion": "sin_clasificar",
        "status": "paused",
        "pausa_hasta": pausa_hasta,
        "processed_at": datetime.now().isoformat(),
    }