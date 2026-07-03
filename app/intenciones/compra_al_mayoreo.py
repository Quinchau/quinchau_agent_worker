"""
Intención: intencion_compra_al_mayoreo
"""
import logging
from datetime import datetime

from ..ghl import send_multiple_messages
from .context import IntentContext, registrar

logger = logging.getLogger(__name__)


@registrar("intencion_compra_al_mayoreo")
def handle(ctx: IntentContext) -> dict:
    logger.info("📦 Compra al mayoreo detectada → respuesta directa")

    mensajes = [
        "📋 Para compras de mayor puede descargar nuestros listados y hacer su pedido en excel",
        "https://quinchau.com/downloader",
        "🔥Aqui encuentras el listado de ofertas",
        "https://quinchau.com/ofertas",
    ]

    send_multiple_messages(ctx.contact_id, mensajes, ctx.channel, delay=1.0)

    ctx.state_manager.update_state(ctx.contact_id, {
        'ultima_intencion': ctx.intencion,
        'entidades_no_resueltas': [],
    })

    return {
        "success": True,
        "response": mensajes,
        "contact_id": ctx.contact_id,
        "intencion": ctx.intencion,
        "processed_at": datetime.now().isoformat(),
    }