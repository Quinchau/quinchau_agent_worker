"""
Resolución de catálogo: consulta al backend, filtrado por LLM cuando hay
varios resultados, y obtención de la URL de catálogo de un modelo.

No es un manejador de intención en sí — es lógica compartida que usa
`compra.py` (y que antes vivía suelta en tasks.py).
"""
import json
import logging
import os
from datetime import datetime

import httpx
from openai import OpenAI

from ..agent_state import AgentStateManager
from ..ghl import send_message_to_ghl, send_multiple_messages
from ..prompts import load_prompt

logger = logging.getLogger(__name__)

CATALOG_ENDPOINT = os.getenv("CATALOG_ENDPOINT", "http://backend:8000/products/internal/resolve-by-entities")
CATALOG_URL_ENDPOINT = os.getenv("CATALOG_URL_ENDPOINT", "http://quinchau-api:3003/api/agent/catalog-url")
CATALOG_TIMEOUT = float(os.getenv("CATALOG_TIMEOUT", "3.0"))


def get_catalog_url_for_model(modelo: str) -> dict:
    """
    Obtiene la URL del catálogo para un modelo específico usando el endpoint dedicado.
    GET /api/agent/catalog-url/:modelo
    """
    try:
        with httpx.Client(timeout=CATALOG_TIMEOUT) as client:
            response = client.get(f"{CATALOG_URL_ENDPOINT}/{modelo}")
            response.raise_for_status()
            data = response.json()

            if data.get('success') and data.get('data'):
                catalog_data = data['data']
                return {
                    'found': True,
                    'url': catalog_data.get('url'),
                    'modelo': catalog_data.get('modelo'),
                    'idmodelo': catalog_data.get('idmodelo'),
                    'marca': catalog_data.get('marca'),
                    'modeldescrip': catalog_data.get('modeldescrip'),
                }
            return None
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            logger.warning(f"⚠️ Modelo '{modelo}' no encontrado en el catálogo")
        else:
            logger.error(f"❌ Error HTTP obteniendo URL del catálogo: {e}")
        return None
    except Exception as e:
        logger.error(f"❌ Error obteniendo URL del catálogo para '{modelo}': {e}")
        return None


def resolver_y_responder_catalogo(state, contact_id, intencion, channel):
    """
    Resuelve producto+modelo y DELEGA al LLM la selección del producto correcto.
    🔥 SOLO ENVÍA LA URL - NADA DE TEXTO ADICIONAL

    ✅ DESPUÉS DE RESOLVER:
    - Limpia el producto (ya se consultó)
    - Mantiene el modelo para contexto (el usuario puede seguir preguntando)
    """
    try:
        producto = state.get('producto')
        modelo = state.get('modelo')

        if not producto or not modelo:
            logger.warning(f"⚠️ Faltan producto o modelo para {contact_id}")
            return None

        logger.info(f"🔍 Resolviendo catálogo: producto='{producto}', modelo='{modelo}'")

        with httpx.Client(timeout=CATALOG_TIMEOUT) as client:
            response = client.post(
                CATALOG_ENDPOINT,
                json={"identidad_producto": producto, "identidad_modelo": modelo},
            )
            response.raise_for_status()
            data = response.json()

        resultados = data.get('results', [])

        if not resultados:
            catalog_url = data.get('url')

            if catalog_url:
                mensajes = [
                    f"No encontré '{producto}' específicamente para {modelo.upper()}. "
                    f"Te invito a revisar el catálogo completo de {modelo.upper()}:",
                    catalog_url,
                ]
                logger.info(f"📦 URL del catálogo enviada: {catalog_url}")
            else:
                mensajes = [
                    f"No encontré '{producto}' para {modelo.upper()}. "
                    f"Por favor, verifica el nombre del producto.",
                    "https://quinchau.com/repuestos-motos",
                ]

            send_multiple_messages(contact_id, mensajes, channel, delay=0.5)

            state_manager = AgentStateManager()
            state_manager.update_state(contact_id, {
                'producto': None,
                'entidades_no_resueltas': [],
                'ultimo_producto_consultado': producto,
                'ultimo_modelo_consultado': modelo,
                'ultimo_catalogo_enviado': catalog_url if catalog_url else None,
            })
            logger.info("🧹 Producto limpiado (no encontrado), modelo mantenido para contexto")

            return {
                "success": True,
                "response": mensajes,
                "contact_id": contact_id,
                "intencion": intencion,
                "fallback": True,
                "catalogo_url": catalog_url if catalog_url else None,
                "processed_at": datetime.now().isoformat(),
            }

        # ============================================
        # CASO 1: UN SOLO PRODUCTO - URL DIRECTA
        # ============================================
        if len(resultados) == 1:
            url = resultados[0].get('url', '')
            respuesta = url

            send_message_to_ghl(contact_id, respuesta, channel)

            state_manager = AgentStateManager()
            state_manager.update_state(contact_id, {
                'producto': None,
                'entidades_no_resueltas': [],
                'ultimo_producto_consultado': producto,
                'ultimo_modelo_consultado': modelo,
            })
            logger.info("🧹 Producto limpiado (resuelto), modelo mantenido para contexto")

            return {
                "success": True,
                "response": respuesta,
                "contact_id": contact_id,
                "intencion": intencion,
                "producto_resuelto": producto,
                "modelo_contexto": modelo,
                "total_resultados": 1,
                "processed_at": datetime.now().isoformat(),
            }

        # ============================================
        # CASO 2: MÚLTIPLES PRODUCTOS - LLM FILTRA
        # ============================================

        productos_texto = ""
        for i, item in enumerate(resultados[:10], 1):
            productos_texto += f"{i}. {item.get('description')} (Código: {item.get('stockid')}) - Stock: {item.get('stock', 0)} unidades\n"
            productos_texto += f"   URL: {item.get('url')}\n\n"

        historial_texto = ""
        turnos = state.get('ultimos_turnos', [])[-3:]
        if turnos:
            historial_texto = "Historial reciente:\n"
            for t in turnos:
                historial_texto += f"Cliente: {t['cliente']}\n"
                historial_texto += f"Asistente: {t['asistente']}\n\n"

        prompt_seleccion = load_prompt(
            "prompt_seleccion_catalogo",
            nombre_cliente=state.get('nombre_cliente', 'Cliente'),
            producto=producto,
            modelo=modelo,
            intencion=intencion,
            historial_texto=historial_texto,
            productos_texto=productos_texto,
        )

        llm_client = OpenAI(
            base_url="https://openrouter.ai/api/v1",
            api_key=os.getenv("OPENROUTER_API_KEY"),
        )

        seleccion_response = llm_client.chat.completions.create(
            model="openai/gpt-4o-mini",
            messages=[{"role": "user", "content": prompt_seleccion}],
            temperature=0.2,
            response_format={"type": "json_object"},
        )

        seleccion = json.loads(seleccion_response.choices[0].message.content)

        if seleccion.get('seleccionado', False):
            url = seleccion.get('url', '')
            stockid = seleccion.get('stockid', '')
            description = seleccion.get('description', '')
            stock = seleccion.get('stock', 0)
            razon = seleccion.get('razon', '')

            respuesta = url

            logger.info(f"🧠 LLM seleccionó: {description} (Código: {stockid})")
            logger.info(f"   Stock: {stock}")
            logger.info(f"   Razón: {razon}")
        else:
            logger.warning(f"⚠️ LLM no seleccionó, usando el primer producto de {len(resultados)} resultados")

            primer_producto = resultados[0]
            url = primer_producto.get('url', '')
            stockid = primer_producto.get('stockid', '')
            description = primer_producto.get('description', '')
            stock = primer_producto.get('stock', 0)

            respuesta = url

            logger.info(f"📦 Fallback: usando {description} (Código: {stockid}) - Stock: {stock}")

        send_message_to_ghl(contact_id, respuesta, channel)

        state_manager = AgentStateManager()
        state_manager.update_state(contact_id, {
            'producto': None,
            'entidades_no_resueltas': [],
            'ultimo_producto_consultado': producto,
            'ultimo_modelo_consultado': modelo,
        })
        logger.info(f"🧹 Producto limpiado (resuelto), modelo '{modelo}' mantenido para contexto")

        return {
            "success": True,
            "response": respuesta,
            "contact_id": contact_id,
            "intencion": intencion,
            "seleccionado": seleccion.get('seleccionado', False),
            "producto_seleccionado": seleccion.get('stockid') if seleccion.get('seleccionado') else stockid,
            "producto_resuelto": producto,
            "modelo_contexto": modelo,
            "total_resultados": len(resultados),
            "processed_at": datetime.now().isoformat(),
        }

    except Exception as e:
        logger.error(f"❌ Error en resolución de catálogo: {str(e)}")
        import traceback
        logger.error(traceback.format_exc())
        return None