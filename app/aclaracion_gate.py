# app/aclaracion_gate.py

import json
import logging
import os
from openai import OpenAI
from .prompts import load_prompt
from .catalog_cache import CatalogCache

logger = logging.getLogger(__name__)

_client = OpenAI(
    base_url="https://openrouter.ai/api/v1",
    api_key=os.getenv("OPENROUTER_API_KEY"),
)

PROMPT_NOMBRE = "prompt_aclaracion_gate"


def es_aclaracion_de_busqueda(message: str, historial_texto: str, state: dict) -> bool:
    """
    Devuelve True si `message` es una aclaración/especificación sobre el
    último producto+modelo ya resuelto para este contacto.
    Usa LLM para clasificar, con un prompt específico.
    """
    ultimo_producto = state.get('ultimo_producto_consultado')
    ultimo_modelo = state.get('ultimo_modelo_consultado')

    if not ultimo_producto or not ultimo_modelo:
        logger.info("🔁 Gate aclaración: No hay producto/modelo previo → false")
        return False

    try:
        # ============================================================
        # OBTENER HERRAMIENTAS DESDE REDIS/CATALOG CACHE
        # ============================================================
        catalog_cache = CatalogCache()
        herramientas = catalog_cache.get_herramientas()
        
        # Lista de nombres de herramientas
        herramientas_nombres = [t['function']['name'] for t in herramientas]
        herramientas_texto = "\n".join([f"  - {nombre}" for nombre in herramientas_nombres])
        
        # Descripción de herramientas (desde el catálogo)
        herramientas_descripcion = []
        for t in herramientas:
            nombre = t['function']['name']
            descripcion = t['function'].get('description', '')
            herramientas_descripcion.append(f"  - {nombre}: {descripcion}")
        herramientas_descripcion_texto = "\n".join(herramientas_descripcion)
        
        # ============================================================
        # CARGAR PROMPT CON HERRAMIENTAS DINÁMICAS
        # ============================================================
        system_prompt = load_prompt(
            PROMPT_NOMBRE,
            ultimo_producto=ultimo_producto,
            ultimo_modelo=ultimo_modelo,
            message=message,
            herramientas_disponibles=herramientas_texto,
            herramientas_descripcion=herramientas_descripcion_texto,
        )
        
        logger.info(f"📝 PROMPT GATE ACLARACIÓN:")
        logger.info(system_prompt)
        
        response = _client.chat.completions.create(
            model="openai/gpt-4o-mini",
            messages=[{"role": "user", "content": system_prompt}],
            temperature=0.0,
            response_format={"type": "json_object"},
        )
        
        data = json.loads(response.choices[0].message.content)
        resultado = bool(data.get("es_aclaracion", False))
        
        logger.info(f"🔁 Gate aclaración: {resultado} (contexto: {ultimo_producto}/{ultimo_modelo})")
        return resultado
        
    except Exception as e:
        logger.warning(f"⚠️ Gate de aclaración falló, degradando a flujo normal: {e}")
        return False