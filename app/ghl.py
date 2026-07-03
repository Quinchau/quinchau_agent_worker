"""
Helpers de envío de mensajes a GoHighLevel (GHL).
"""
import logging
import os
import time

import httpx

logger = logging.getLogger(__name__)


def send_message_to_ghl(contact_id: str, message: str, channel: str = "WhatsApp") -> dict:
    """
    Envía la respuesta generada por el LLM de vuelta al contacto en GHL
    usando la Conversations API (Send a new message).
    """
    token = os.getenv("GHL_PRIVATE_TOKEN")
    if not token:
        raise ValueError("GHL_PRIVATE_TOKEN no configurada")

    url = "https://services.leadconnectorhq.com/conversations/messages"
    headers = {
        "Authorization": f"Bearer {token}",
        "Version": "2021-04-15",
        "Content-Type": "application/json",
    }
    payload = {
        "type": channel,
        "contactId": contact_id,
        "message": message,
    }

    resp = httpx.post(url, json=payload, headers=headers, timeout=10)
    resp.raise_for_status()
    return resp.json()


def send_multiple_messages(contact_id, messages, channel, delay: float = 0.5) -> None:
    """Envía múltiples mensajes consecutivos a GHL."""
    for i, msg in enumerate(messages):
        if not msg or not msg.strip():
            continue
        send_message_to_ghl(contact_id, msg.strip(), channel)
        logger.info(f"📤 Mensaje {i+1}/{len(messages)} enviado: {msg[:50]}...")
        if i < len(messages) - 1:
            time.sleep(delay)