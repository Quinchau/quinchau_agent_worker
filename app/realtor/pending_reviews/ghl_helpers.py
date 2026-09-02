import logging
import os
from datetime import datetime, timezone

import httpx

logger = logging.getLogger(__name__)


def actualizar_ultima_revision_ia(contact_id: str) -> None:
    """
    Actualiza el custom field 'ultima_revision_ia' en GHL con la fecha/hora actual.
    """
    # Usamos las variables genéricas o las específicas de Miami si existen
    api_key = os.environ.get("GHL_PRIVATE_TOKEN_FRANCHESCA_QUINTERO_TEAM_MIAMI") or os.environ.get("GHL_PRIVATE_TOKEN")
    cf_id = os.environ.get("GHL_CF_ULTIMA_REVISION_IA_ID")
    
    if not api_key or not cf_id:
        logger.warning("⚠️ Faltan credenciales de GHL (GHL_PRIVATE_TOKEN o GHL_CF_ULTIMA_REVISION_IA_ID) para actualizar custom field")
        return
    
    url = f"https://services.leadconnectorhq.com/contacts/{contact_id}"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Version": "2021-07-28",
        "Content-Type": "application/json",
    }
    
    payload = {
        "customFields": [
            {
                "id": cf_id,
                "value": datetime.now(timezone.utc).isoformat()
            }
        ]
    }
    
    try:
        response = httpx.put(url, headers=headers, json=payload, timeout=10.0)
        response.raise_for_status()
        logger.info(f"✅ Custom field 'ultima_revision_ia' actualizado para {contact_id}")
    except Exception as e:
        logger.error(f"❌ Error actualizando custom field para {contact_id}: {e}")