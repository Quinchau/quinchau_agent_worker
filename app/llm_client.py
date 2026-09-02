# app/llm_client.py
"""
Cliente LLM compartido (infra, domain-agnostic).

Usado tanto por app/classifier.py (dominio motos) como por
app/realtor/cool_leads/reasoning.py (dominio realtor).

Mismo proveedor (OpenRouter) y misma API key para ambos dominios —
la diferencia de modelo/temperatura/tokens se define por dominio
vía env vars (CLASSIFIER_* vs COOL_LEADS_*), no acá.
"""

import logging
import os
from functools import lru_cache

from openai import OpenAI

logger = logging.getLogger(__name__)

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"


@lru_cache(maxsize=1)
def get_openrouter_client() -> OpenAI:
    """
    Devuelve una única instancia de OpenAI apuntando a OpenRouter,
    cacheada a nivel de proceso (lru_cache) para no reinstanciar
    el cliente en cada llamada.
    """
    api_key = os.environ.get("OPENROUTER_API_KEY")
    if not api_key:
        raise RuntimeError(
            "OPENROUTER_API_KEY no está seteada en el entorno del Worker. "
            "Es la misma variable que ya usa app/classifier.py."
        )

    logger.info("🔌 Inicializando cliente OpenRouter (compartido entre dominios)")

    return OpenAI(
        base_url=OPENROUTER_BASE_URL,
        api_key=api_key,
        default_headers={
            "HTTP-Referer": "https://quinchau.app",
            "X-Title": "QuinChau Agent",
        },
    )