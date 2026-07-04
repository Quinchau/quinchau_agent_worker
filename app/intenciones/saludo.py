"""
Intención: intencion_saludo
"""
import logging
from datetime import datetime

from ..ghl import send_message_to_ghl
from ..prompts import load_prompt
from .context import IntentContext, registrar

logger = logging.getLogger(__name__)


@registrar("intencion_saludo")
def handle(ctx: IntentContext) -> dict:
    logger.info(f"👋 Saludo o agradecimiento detectado - {ctx.first_name} {ctx.last_name}")

    system_prompt = load_prompt(
        "prompt_intencion_saludo",
        first_name=ctx.first_name,
        historial_texto=ctx.historial_texto,
    )

    try:
        llm_response = ctx.client.chat.completions.create(
            model="openai/gpt-4o-mini",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": f"El cliente {ctx.first_name} dice: \"{ctx.message}\""},
            ],
            temperature=0.3,
            max_tokens=60,
        )
        respuesta = llm_response.choices[0].message.content.strip()
    except Exception as e:
        logger.error(f"❌ Error generando saludo: {e}")
        respuesta = f"¡Hola {ctx.first_name}! ¿En qué puedo ayudarte hoy?"

    logger.info(f"✅ Saludo enviado: {respuesta[:40]}...")

    send_message_to_ghl(ctx.contact_id, respuesta, ctx.channel)
    # ❌ ELIMINADA: ctx.state_manager.add_turno(ctx.contact_id, ctx.message, respuesta)
    ctx.state_manager.update_state(ctx.contact_id, {'ultima_intencion': ctx.intencion})

    return {
        "success": True,
        "response": respuesta,
        "contact_id": ctx.contact_id,
        "intencion": ctx.intencion,
        "processed_at": datetime.now().isoformat(),
    }