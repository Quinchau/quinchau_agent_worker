"""
Manejador genérico: se usa cuando la intención no tiene una rama propia, o
cuando un manejador específico decide (retornando None) que prefiere caer
en este comportamiento por defecto — p. ej. `compra.py` si el catálogo
falla.

A diferencia de los demás manejadores, este NO se registra con
`@registrar`: se llama explícitamente desde tasks.py como último recurso.
"""
import logging
from datetime import datetime

from ..ghl import send_message_to_ghl
from ..prompts import load_prompt
from .context import IntentContext

logger = logging.getLogger(__name__)


def handle(ctx: IntentContext) -> dict:
    entidades_texto = ""
    if ctx.state.get('producto'):
        entidades_texto += f"- Producto: {ctx.state['producto']}\n"
    if ctx.state.get('modelo'):
        entidades_texto += f"- Modelo de moto: {ctx.state['modelo']}\n"
    if ctx.state.get('ultimo_modelo') and ctx.state.get('ultimo_modelo') != ctx.state.get('modelo'):
        entidades_texto += f"- Último modelo mencionado: {ctx.state['ultimo_modelo']}\n"
    if ctx.state.get('ubicacion'):
        entidades_texto += f"- Ubicación consultada: {ctx.state['ubicacion']}\n"
    if ctx.state.get('envio'):
        entidades_texto += f"- Envío consultado: {ctx.state['envio']}\n"
    if ctx.state.get('pago'):
        entidades_texto += f"- Método de pago consultado: {ctx.state['pago']}\n"
    if not entidades_texto:
        entidades_texto = "Aún no tenemos información específica del cliente en esta conversación."

    turnos = ctx.state.get('ultimos_turnos', [])[-4:]
    historial_texto = ""
    if turnos:
        historial_texto = "Historial reciente de la conversación:\n"
        for t in turnos:
            historial_texto += f"Cliente: {t['cliente']}\n"
            historial_texto += f"Asistente: {t['asistente']}\n\n"
    if not historial_texto:
        historial_texto = "No hay historial reciente de conversación."

    system_prompt = load_prompt(
        "prompt_llm_generico_system",
        first_name=ctx.first_name,
        last_name=ctx.last_name,
        contact_id=ctx.contact_id,
        entidades_texto=entidades_texto,
        historial_texto=historial_texto,
    )

    user_prompt = load_prompt(
        "prompt_llm_generico_user",
        message=ctx.message,
        intencion=ctx.intencion,
    )

    logger.info("=" * 60)
    logger.info("📝 PROMPT DEL LLM:")
    logger.info("-" * 40)
    logger.info(f"🧠 System: {system_prompt.strip()[:200]}...")
    logger.info(f"👤 User: {ctx.message}")
    logger.info("=" * 60)

    logger.info("🔄 Generando respuesta...")

    response = ctx.client.chat.completions.create(
        model="openai/gpt-4o-mini",
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        temperature=0.3,
        max_tokens=300,
    )

    llm_response = response.choices[0].message.content

    logger.info("=" * 60)
    logger.info("📨 RESPUESTA DEL LLM:")
    logger.info("-" * 40)
    logger.info(llm_response)
    logger.info("=" * 60)

    ctx.state_manager.update_state(ctx.contact_id, {
        "ultima_intencion": ctx.intencion,
        "ultimo_modelo": ctx.state.get('modelo'),
    })

    send_message_to_ghl(ctx.contact_id, llm_response, ctx.channel)
    logger.info("📤 Respuesta enviada a GHL")

    return {
        "success": True,
        "response": llm_response,
        "contact_id": ctx.contact_id,
        "intencion": ctx.intencion,
        "entidades_resueltas": ctx.resolution.get('resolved', {}),
        "entidades_no_resueltas": ctx.resolution.get('no_resueltas', []),
        "processed_at": datetime.now().isoformat(),
    }