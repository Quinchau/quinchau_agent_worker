import json
import logging
from datetime import datetime
from typing import Dict, List, Optional

from ..ghl import send_message_to_ghl, send_multiple_messages
from ..catalog_cache import catalog_cache
from ..entity_resolver import entity_resolver
from ..prompts import load_prompt
from .catalogo import get_catalog_url_for_model
from .context import IntentContext, registrar

logger = logging.getLogger(__name__)

MAX_INTENTOS_MODELO = 3


@registrar("intencion_compra")
def handle(ctx: IntentContext) -> Optional[dict]:
    logger.info("🔍 Procesando consulta de catálogo (compra/disponibilidad/precio)")

    # ============================================
    # 1. RESOLVER MODELO (con reintentos acotados)
    # ============================================
    modelo = ctx.resolution.get('modelo')

    if not modelo:
        texto_modelo_llm = ctx.entidades_detectadas.get('modelo', '')
        resultado_reintento = entity_resolver.resolver_modelo(texto_modelo_llm) if texto_modelo_llm else None

        if resultado_reintento:
            modelo = resultado_reintento['modelo']
            ctx.state_manager.update_state(ctx.contact_id, {
                'modelo': modelo,
                'alias_modelo': resultado_reintento['alias'],
                'ultimo_modelo': modelo,
                'model_found': True,
                'intentos_resolucion': 0,
                'updated_at': datetime.now().isoformat(),
            })
            logger.info(f"✅ Modelo resuelto en segunda pasada: '{modelo}'")
        else:
            intentos = ctx.state.get('intentos_resolucion', 0) + 1
            ctx.state_manager.update_state(ctx.contact_id, {
                'intentos_resolucion': intentos,
                'model_found': False,
                'updated_at': datetime.now().isoformat(),
            })
            logger.info(f"⚠️ Modelo no resuelto. Intento {intentos}/{MAX_INTENTOS_MODELO}")

            if intentos < MAX_INTENTOS_MODELO:
                mensaje = "No logré identificar el modelo de tu moto. ¿Podrías decirme cuál es?"
                send_message_to_ghl(ctx.contact_id, mensaje, ctx.channel)
                return {
                    "success": True,
                    "response": mensaje,
                    "contact_id": ctx.contact_id,
                    "intencion": ctx.intencion,
                    "intentos_resolucion": intentos,
                    "processed_at": datetime.now().isoformat(),
                }
            else:
                # Se agotaron los reintentos: fallback definitivo, deriva a catálogo general.
                logger.info("⛔ Reintentos de modelo agotados, derivando a catálogo general")
                ctx.state_manager.update_state(ctx.contact_id, {
                    'intentos_resolucion': 0,
                    'esperando_confirmacion': False,
                    'esperando_respuesta': False,
                })
                mensaje = (
                    "No logré identificar el modelo de tu moto. "
                    "Te dejo el catálogo general para que busques directamente:"
                )
                mensajes = [mensaje, "https://quinchau.com/repuestos-motos"]
                send_multiple_messages(ctx.contact_id, mensajes, ctx.channel, delay=0.5)
                return {
                    "success": True,
                    "response": mensajes,
                    "contact_id": ctx.contact_id,
                    "intencion": ctx.intencion,
                    "fallback": True,
                    "fallback_tipo": "modelo_no_resuelto",
                    "processed_at": datetime.now().isoformat(),
                }

    # ============================================
    # 2. OBTENER CATÁLOGO DEL MODELO (recién acá, ya con modelo confirmado)
    # ============================================
    productos_modelo = catalog_cache.get_productos_por_modelo(modelo)

    if not productos_modelo:
        logger.warning(f"⚠️ Modelo '{modelo}' sin productos en catálogo")
        return _fallback_catalogo_modelo(ctx, modelo)

    # ============================================
    # 3. SEGUNDA LLAMADA AL LLM — SELECCIÓN DE PRODUCTO(S)
    # ============================================
    ids_seleccionados = _resolver_productos_llm(ctx, modelo, productos_modelo)

    productos_resueltos = [p for p in productos_modelo if p.get('id') in ids_seleccionados]

    if not productos_resueltos:
        return _fallback_catalogo_modelo(ctx, modelo)

    # ============================================
    # FILTRO POR DISPONIBILIDAD + ARMADO DE MENSAJES
    # ============================================
    disponibles = [p for p in productos_resueltos if str(p.get('stock', '')).strip().lower() == 'disponible']

    if disponibles:
        productos_a_enviar = disponibles
        mensajes = [
            f"{p['url']}\n*_DISPONIBLE_*, COMPRAR 👆"
            for p in productos_a_enviar if p.get('url')
        ]
        logger.info(f"📦 {len(disponibles)}/{len(productos_resueltos)} disponibles, enviando disponibles")
    else:
        producto_agotado = max(productos_resueltos, key=lambda p: p.get('precio', 0) or 0)
        productos_a_enviar = [producto_agotado]
        mensajes = [
            f"{producto_agotado['url']}\n*_AGOTADO_*, Suscríbete para avisarte cuando llegue."
        ] if producto_agotado.get('url') else []
        logger.info(f"⚠️ Sin stock disponible, enviando agotado más relevante: {producto_agotado.get('id')}")

    if not mensajes:
        logger.warning("⚠️ Productos resueltos sin URL, usando fallback de modelo")
        return _fallback_catalogo_modelo(ctx, modelo)

    send_multiple_messages(ctx.contact_id, mensajes, ctx.channel, delay=0.5)

    ctx.state_manager.update_state(ctx.contact_id, {
        "esperando_confirmacion": False,
        "esperando_respuesta": False,
    })

    logger.info(f"📤 {len(mensajes)} producto(s) enviado(s) para modelo '{modelo}'")

    return {
        "success": True,
        "response": mensajes,
        "contact_id": ctx.contact_id,
        "intencion": ctx.intencion,
        "modelo_contexto": modelo,
        "total_productos": len(mensajes),
        "hubo_disponibles": bool(disponibles),
        "processed_at": datetime.now().isoformat(),
    }


def _resolver_productos_llm(ctx: IntentContext, modelo: str, productos_modelo: List[Dict]) -> List[str]:
    """
    Segunda llamada al LLM: dado el catálogo real del modelo, decide qué
    id(s) coinciden con lo que pidió el cliente. El LLM nunca devuelve
    datos del producto (nombre/url/stock) — solo ids, que luego se
    resuelven por lookup contra productos_modelo.
    """
    productos_texto = "\n".join(
        f"- {p.get('id')}: {p.get('nombre', '')}"
        for p in productos_modelo
        if p.get('id')
    )

    producto_pedido = ctx.entidades_detectadas.get('producto', '')

    system_prompt = load_prompt(
        "prompt_seleccion_catalogo",
        nombre_cliente=ctx.first_name,
        producto=producto_pedido,
        modelo=modelo,
        intencion=ctx.intencion,
        historial_texto=ctx.historial_texto,
        productos_texto=productos_texto,
    )

    logger.info(
        f"📝 Prompt selección catálogo armado | modelo={modelo} | "
        f"productos_en_catalogo={len(productos_modelo)} | "
        f"producto_pedido={producto_pedido!r}"
    )

    try:
        response = ctx.client.chat.completions.create(
            model="openai/gpt-4o-mini",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": ctx.message},
            ],
            temperature=0.1,
            response_format={"type": "json_object"},
        )
        contenido = response.choices[0].message.content
        data = json.loads(contenido)
        ids = data.get('ids_seleccionados', [])
        razon = data.get('razon', '')

        if not isinstance(ids, list):
            logger.warning(f"⚠️ LLM devolvió 'ids_seleccionados' como no-lista: {ids!r}")
            ids = [ids] if ids else []

        logger.info(f"🎯 Ids seleccionados: {ids} | Razón: {razon}")
        return ids

    except (json.JSONDecodeError, KeyError, IndexError) as e:
        logger.error(f"❌ Error parseando respuesta de selección de catálogo: {e}")
        return []


def _fallback_catalogo_modelo(ctx: IntentContext, modelo: str) -> dict:
    """Modelo resuelto, ningún producto matchea. Respuesta final del turno, sin reintentos."""
    catalog_info = get_catalog_url_for_model(modelo)

    if catalog_info and catalog_info.get('found'):
        catalogo_url = catalog_info.get('url')
        modeldescrip = catalog_info.get('modeldescrip', modelo)
        mensajes = [
            f"No encontré ese producto específico para {modeldescrip}. "
            f"Te invito a revisar el catálogo completo:",
            catalogo_url,
        ]
    else:
        catalogo_url = None
        mensajes = [
            f"No encontré ese producto para {modelo}. Te invito a revisar el catálogo general:",
            "https://quinchau.com/repuestos-motos",
        ]

    send_multiple_messages(ctx.contact_id, mensajes, ctx.channel, delay=0.5)

    ctx.state_manager.update_state(ctx.contact_id, {
        "esperando_confirmacion": False,
        "esperando_respuesta": False,
    })

    return {
        "success": True,
        "response": mensajes,
        "contact_id": ctx.contact_id,
        "intencion": ctx.intencion,
        "fallback": True,
        "fallback_tipo": "producto_no_encontrado",
        "modelo_contexto": modelo,
        "catalogo_url": catalogo_url,
        "processed_at": datetime.now().isoformat(),
    }