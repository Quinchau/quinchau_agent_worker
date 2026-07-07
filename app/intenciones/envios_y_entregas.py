"""
Intención: envios_y_entregas
"""
import logging
import re
from datetime import datetime

from ..ghl import send_message_to_ghl
from ..prompts import load_prompt
from .context import IntentContext, registrar

logger = logging.getLogger(__name__)


@registrar("envios_y_entregas")
def handle(ctx: IntentContext) -> dict:
    logger.info("📦 Consulta de envíos y entregas detectada")
    
    nombre = ctx.state.get('nombre_cliente', 'Cliente')
    mensaje = ctx.message.lower()

    # Detectar ubicación
    ciudad = "tu ubicación"
    if "limón" in mensaje or "limon" in mensaje:
        ciudad = "El Limón"
    elif "maracay" in mensaje:
        ciudad = "Maracay"
    else:
        # Extraer con regex
        match = re.search(r'(?:en|a|para)\s+([A-Za-záéíóúñ\s]+)', ctx.message, re.IGNORECASE)
        if match:
            ciudad = match.group(1).strip()

    prompt_envios = load_prompt(
        "prompt_envios_y_entregas",
        nombre=nombre,
        ciudad=ciudad,
    )

    respuesta = ctx.client.chat.completions.create(
        model="openai/gpt-4o-mini",
        messages=[{"role": "user", "content": prompt_envios}],
        temperature=0.5,
        max_tokens=100,
    ).choices[0].message.content.strip()

    send_message_to_ghl(ctx.contact_id, respuesta, ctx.channel)

    ctx.state_manager.update_state(ctx.contact_id, {
        'ultima_intencion': ctx.intencion,
        'ubicacion': ciudad,
    })

    logger.info(f"📤 Respuesta de envíos enviada para {ciudad}")

    return {
        "success": True,
        "response": respuesta,
        "contact_id": ctx.contact_id,
        "intencion": ctx.intencion,
        "ubicacion": ciudad,
        "processed_at": datetime.now().isoformat(),
    }