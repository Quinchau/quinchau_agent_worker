"""
Handler para la intención busqueda_producto_generica.

Esta rama resuelve ÚNICAMENTE por producto (sin cruce con modelo):
  - El LLM evalúa los candidatos encontrados en el catálogo para el
    término de producto pedido.
  - Si el término es claro, se buscan y envían los productos.
  - Si existe ambigüedad semántica (varias familias de producto distintas
    que calzan con el término), se gestiona la aclaración.
  - Si no hay ningún candidato relacionado, se cae al fallback de catálogo.
"""

import json
import logging
from datetime import datetime
from typing import Dict, List, Optional

from ..ghl import send_message_to_ghl, send_multiple_messages
from ..catalog_cache import catalog_cache
from ..prompts import load_prompt
from .context import IntentContext, registrar

logger = logging.getLogger(__name__)

MAX_ACLARACIONES_PRODUCTO = 4


@registrar("busqueda_producto_generica")
def handle(ctx: IntentContext) -> Optional[dict]:
    logger.info("🔍 Procesando consulta de catálogo por producto")

    # ============================================
    # 1. LEER PRODUCTO YA RESUELTO (POR GATE 2.7)
    # ============================================
    producto_pedido = ctx.resolution.get('producto')

    if not producto_pedido:
        mensaje = "¿Qué producto estás buscando?"
        send_message_to_ghl(ctx.contact_id, mensaje, ctx.channel)
        return {
            "success": True,
            "response": mensaje,
            "contact_id": ctx.contact_id,
            "intencion": ctx.intencion,
            "esperando_producto": True,
            "processed_at": datetime.now().isoformat(),
        }

    # ============================================
    # 2. RETOMAR ACLARACIÓN PENDIENTE
    # ============================================
    aclaracion_pendiente = bool(ctx.state.get('es_aclaracion')) and bool(ctx.state.get('producto_candidatos'))

    if aclaracion_pendiente:
        candidatos_persistidos = ctx.state.get('producto_candidatos') or []

        logger.info(f"♻️ Retomando aclaración pendiente | candidatos_persistidos={len(candidatos_persistidos)}")

        decision = _decidir_accion_llm(
            ctx, producto_pedido, candidatos_persistidos,
            contexto_extra="El cliente ya vio una pregunta de aclaración en el turno anterior. "
                           "Este mensaje es su respuesta a esa pregunta.",
        )
        return _procesar_decision(ctx, producto_pedido, candidatos_persistidos, decision)

    # ============================================
    # 3. CONSULTA DE CATÁLOGO Y EVALUACIÓN POR LLM
    # ============================================
    productos_candidatos = catalog_cache.get_productos_por_modelo(None, producto_pedido)

    if not productos_candidatos:
        logger.warning(f"⚠️ '{producto_pedido}' sin productos en catálogo")
        ctx.state_manager.update_state(ctx.contact_id, {'es_aclaracion': False, 'intentos_producto': 0, 'producto_candidatos': None})
        return _fallback_catalogo(ctx, producto_pedido)

    decision = _decidir_accion_llm(ctx, producto_pedido, productos_candidatos)
    return _procesar_decision(ctx, producto_pedido, productos_candidatos, decision)


# ============================================
# PROCESAMIENTO COMÚN DE DECISIÓN
# ============================================

def _procesar_decision(
    ctx: IntentContext,
    producto_pedido: str,
    productos_candidatos: List[Dict],
    decision: Dict,
) -> dict:
    accion = decision.get("accion")

    if accion == "pedir_aclaracion":
        intentos_prod = ctx.state.get('intentos_producto', 0) + 1

        if intentos_prod > MAX_ACLARACIONES_PRODUCTO:
            logger.warning("⛔ Circuit breaker: demasiadas aclaraciones sin resolver, forzando fallback")
            ctx.state_manager.update_state(ctx.contact_id, {'es_aclaracion': False, 'intentos_producto': 0, 'producto_candidatos': None})
            return _fallback_catalogo(ctx, producto_pedido)

        ctx.state_manager.update_state(ctx.contact_id, {
            'es_aclaracion': True,
            'intentos_producto': intentos_prod,
            'producto_candidatos': productos_candidatos,
            'updated_at': datetime.now().isoformat(),
        })
        mensaje = decision.get("mensaje_aclaracion") or "¿Qué producto estás necesitando?"
        send_message_to_ghl(ctx.contact_id, mensaje, ctx.channel)
        return {
            "success": True,
            "response": mensaje,
            "contact_id": ctx.contact_id,
            "intencion": ctx.intencion,
            "esperando_producto": True,
            "processed_at": datetime.now().isoformat(),
        }

    if accion == "fallback_catalogo":
        ctx.state_manager.update_state(ctx.contact_id, {'es_aclaracion': False, 'intentos_producto': 0, 'producto_candidatos': None})
        return _fallback_catalogo(ctx, producto_pedido, mensaje_intro=decision.get("mensaje_fallback"))

    # accion == "buscar_producto"
    ctx.state_manager.update_state(ctx.contact_id, {'es_aclaracion': False, 'intentos_producto': 0, 'producto_candidatos': None})
    ids_seleccionados = decision.get("ids_seleccionados", [])
    productos_resueltos = [p for p in productos_candidatos if p.get('id') in ids_seleccionados]

    if not productos_resueltos:
        return _fallback_catalogo(ctx, producto_pedido)

    return _enviar_productos_resueltos(ctx, producto_pedido, productos_resueltos)


def _enviar_productos_resueltos(
    ctx: IntentContext,
    producto_pedido: str,
    productos_resueltos: List[Dict],
) -> dict:
    disponibles = [p for p in productos_resueltos if str(p.get('stock', '')).strip().lower() == 'disponible']

    if disponibles:
        productos_a_enviar = disponibles
        mensajes = [
            f"{p['url']}\n*_DISPONIBLE_*, COMPRAR 👆"
            for p in productos_a_enviar if p.get('url')
        ]
        logger.info(f"📦 {len(disponibles)}/{len(productos_resueltos)} disponibles, enviando disponibles")
    else:
        productos_a_enviar = productos_resueltos
        mensajes = [
            "Estos productos están agotados por el momento. Puedes hacer clic en ellos "
            "para suscribirte y te avisamos apenas vuelvan a estar disponibles — "
            "*_esto no implica ninguna obligación de compra._*"
        ]

        for p in productos_a_enviar:
            if p.get('url'):
                mensajes.append(f"{p['url']}\n*_AGOTADO_*, Suscríbete para avisarte cuando llegue.")

        ids_agotados = [p.get('id') for p in productos_a_enviar if p.get('id')]
        logger.info(f"⚠️ Sin stock disponible, enviando repuestos agotados: {ids_agotados}")

    if not mensajes:
        logger.warning("⚠️ Productos resueltos sin URL, usando fallback")
        return _fallback_catalogo(ctx, producto_pedido)

    send_multiple_messages(ctx.contact_id, mensajes, ctx.channel, delay=0.5)

    ctx.state_manager.update_state(ctx.contact_id, {
        "esperando_confirmacion": False,
        "esperando_respuesta": False,
    })

    logger.info(f"📤 {len(mensajes)} producto(s) enviado(s) para '{producto_pedido}'")

    return {
        "success": True,
        "response": mensajes,
        "contact_id": ctx.contact_id,
        "intencion": ctx.intencion,
        "producto_contexto": producto_pedido,
        "total_productos": len(mensajes),
        "hubo_disponibles": bool(disponibles),
        "processed_at": datetime.now().isoformat(),
    }


# ============================================
# EVALUACIÓN Y DECISIÓN VÍA LLM
# ============================================

def _decidir_accion_llm(
    ctx: IntentContext,
    producto_pedido: str,
    productos_candidatos: List[Dict],
    contexto_extra: Optional[str] = None,
) -> Dict:
    """
    Invoca al LLM enviando los candidatos encontrados para que decida la acción a ejecutar.
    """
    productos_texto = "\n".join(
        f"- {p.get('id')}: {p.get('nombre', '')}"
        for p in productos_candidatos
        if p.get('id')
    )

    system_prompt = load_prompt(
        "prompt_seleccion_catalogo_generica",
        nombre_cliente=ctx.first_name,
        producto=producto_pedido,
        intencion=ctx.intencion,
        historial_texto=ctx.historial_texto,
        productos_texto=productos_texto,
        contexto_extra=contexto_extra or "",
    )

    logger.info(
        f"📝 Prompt decisión de catálogo armado | "
        f"productos_en_catalogo={len(productos_candidatos)} | "
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
        data = json.loads(response.choices[0].message.content)

        ids = data.get('ids_seleccionados', [])
        if not isinstance(ids, list):
            ids = [ids] if ids else []
        data['ids_seleccionados'] = ids
        data['es_aclaracion'] = bool(data.get('es_aclaracion', False))

        logger.info(f"🎯 Acción: {data.get('accion')} | es_aclaracion: {data['es_aclaracion']} | IDs: {ids} | Razón: {data.get('razon', '')}")
        return data

    except (json.JSONDecodeError, KeyError, IndexError) as e:
        logger.error(f"❌ Error parseando respuesta de decisión de catálogo: {e}")
        return {"accion": "fallback_catalogo", "ids_seleccionados": [], "es_aclaracion": False, "razon": "error_parseo"}


# ============================================
# FALLBACK DIRECTO A CATÁLOGO
# ============================================

def _fallback_catalogo(
    ctx: IntentContext,
    producto: Optional[str] = None,
    mensaje_intro: Optional[str] = None,
) -> dict:
    """Respuesta final del turno cuando no hay match posible. Sin reintentos."""
    etiqueta = producto or "ese producto"
    intro = mensaje_intro or (
        f"No encontré {etiqueta} en el catálogo. Te invito a revisar el catálogo general:"
    )
    mensajes = [intro, "https://quinchau.com/repuestos-motos"]

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
        "producto_contexto": producto,
        "catalogo_url": "https://quinchau.com/repuestos-motos",
        "processed_at": datetime.now().isoformat(),
    }