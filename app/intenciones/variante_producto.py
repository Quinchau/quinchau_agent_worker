import json
import logging
from datetime import datetime
from typing import Optional

from ..ghl import send_multiple_messages
from ..catalog_cache import catalog_cache
from ..prompts import load_prompt
from .catalogo import get_catalog_url_for_model
from .context import IntentContext, registrar

logger = logging.getLogger(__name__)

TIPOS_VALIDOS = ("verificar_atributo", "buscar_alternativa")


@registrar("consulta_variante_producto")
def handle(ctx: IntentContext) -> Optional[dict]:
    logger.info("🔍 Procesando consulta de variante/atributo sobre producto ya mostrado")

    modelo = ctx.resolution.get('modelo')
    if not modelo:
        mensaje = "¿Podrías decirme el modelo de tu moto para revisarlo bien?"
        send_multiple_messages(ctx.contact_id, [mensaje], ctx.channel, delay=0.5)
        return {"success": True, "response": [mensaje], "contact_id": ctx.contact_id,
                "intencion": ctx.intencion, "processed_at": datetime.now().isoformat()}

    productos_modelo = catalog_cache.get_productos_por_modelo(modelo)
    if not productos_modelo:
        return _fallback_catalogo_modelo(ctx, modelo)

    resultado = _resolver_variante_llm(ctx, modelo, productos_modelo)

    return _armar_respuesta(ctx, modelo, productos_modelo, resultado)


def _resolver_variante_llm(ctx: IntentContext, modelo: str, productos_modelo: list) -> dict:
    tipo_consulta = ctx.entidades_detectadas.get('tipo_consulta', 'verificar_atributo')
    if tipo_consulta not in TIPOS_VALIDOS:
        logger.warning(f"⚠️ tipo_consulta inesperado: {tipo_consulta!r}, usando default")
        tipo_consulta = 'verificar_atributo'

    atributo = ctx.entidades_detectadas.get('atributo_consultado', '')

    productos_texto = "\n".join(
        f"- {p.get('id')}: {p.get('nombre', '')} (stock: {p.get('stock', 'desconocido')})"
        for p in productos_modelo
        if p.get('id')
    )

    system_prompt = load_prompt(
        "prompt_seleccion_variante",
        nombre_cliente=ctx.first_name,
        modelo=modelo,
        tipo_consulta=tipo_consulta,
        atributo_consultado=atributo,
        historial_texto=ctx.historial_texto,
        productos_texto=productos_texto,
    )

    logger.info(
        f"📝 Prompt variante armado | modelo={modelo} | tipo={tipo_consulta} | "
        f"atributo={atributo!r} | catalogo={len(productos_modelo)}"
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
        logger.info(f"🎯 Resultado variante: {data}")
        return data
    except (json.JSONDecodeError, KeyError, IndexError) as e:
        logger.error(f"❌ Error parseando respuesta de variante: {e}")
        return {"coincide": False, "producto_referencia_id": None, "variante_encontrada_id": None, "razon": "error"}


def _armar_respuesta(ctx: IntentContext, modelo: str, productos_modelo: list, resultado: dict) -> dict:
    lookup = {p.get('id'): p for p in productos_modelo}
    referencia = lookup.get(resultado.get('producto_referencia_id'))
    variante = lookup.get(resultado.get('variante_encontrada_id'))
    coincide = resultado.get('coincide', False)

    if coincide and referencia:
        mensajes = [f"Sí, {referencia['nombre']} corresponde a lo que preguntás.\n{referencia.get('url', '')}"]
    elif variante:
        stock = str(variante.get('stock', '')).strip().lower()
        if stock == 'disponible':
            mensajes = [f"Sí existe: {variante['nombre']}\n{variante.get('url','')}\n*_DISPONIBLE_*, COMPRAR 👆"]
        else:
            mensajes = [f"Esa variante ({variante['nombre']}) existe pero está *_AGOTADA_*. "
                        f"Suscríbete para avisarte cuando llegue."]
    else:
        return _fallback_catalogo_modelo(ctx, modelo)

    send_multiple_messages(ctx.contact_id, mensajes, ctx.channel, delay=0.5)
    ctx.state_manager.update_state(ctx.contact_id, {"esperando_confirmacion": False, "esperando_respuesta": False})

    return {
        "success": True, "response": mensajes, "contact_id": ctx.contact_id,
        "intencion": ctx.intencion, "modelo_contexto": modelo,
        "processed_at": datetime.now().isoformat(),
    }


def _fallback_catalogo_modelo(ctx: IntentContext, modelo: str) -> dict:
    catalog_info = get_catalog_url_for_model(modelo)
    if catalog_info and catalog_info.get('found'):
        mensajes = [
            f"No tengo ese detalle específico para {catalog_info.get('modeldescrip', modelo)}. "
            f"Te dejo el catálogo para que revises directamente:",
            catalog_info.get('url'),
        ]
    else:
        mensajes = ["No encontré esa información. Te dejo el catálogo general:", "https://quinchau.com/repuestos-motos"]

    send_multiple_messages(ctx.contact_id, mensajes, ctx.channel, delay=0.5)
    ctx.state_manager.update_state(ctx.contact_id, {"esperando_confirmacion": False, "esperando_respuesta": False})
    return {
        "success": True, "response": mensajes, "contact_id": ctx.contact_id,
        "intencion": ctx.intencion, "fallback": True, "fallback_tipo": "variante_no_encontrada",
        "modelo_contexto": modelo, "processed_at": datetime.now().isoformat(),
    }