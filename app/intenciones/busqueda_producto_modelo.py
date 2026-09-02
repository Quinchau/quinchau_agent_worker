# app/intenciones/busqueda_producto_modelo.py
"""
Handler para la intención busqueda_producto_modelo.

Se activa cuando el MENSAJE ACTUAL del cliente nombra o cambia un
modelo/categoría. La selección de esta tool YA ES la señal semántica
de que el cliente está definiendo/redefiniendo el modelo — esa
distinción solo puede hacerla el LLM clasificador, no un matching
léxico por alias contra un diccionario conocido.

REGLAS DE ESTADO (el modelo es el contexto ACTUAL, no hay histórico):
- El modelo vigente llega ya resuelto y validado en ctx.state['modelo'],
  escrito por el clasificador unificado en tasks.py. Este handler no
  resuelve modelo por su cuenta.
- Si ctx.state['modelo'] es None: el clasificador no pudo identificar
  un modelo en este turno. Se delega al LLM la decisión de pedir
  aclaración o derivar a humano, leyendo el historial completo.
- No existe 'ultimo_modelo' ni contador de intentos para modelo en el
  estado. El criterio de "¿ya le pedí aclaración antes?" lo resuelve
  un LLM leyendo el historial — igual que un humano recordaría la
  conversación, no contando intentos.

Una vez resuelto el modelo, se mantiene el patrón que ya funcionaba:
se entrega al LLM de catálogo TODA la lista de productos de ese
modelo y es él quien resuelve selección, aclaración o fallback.

RESOLUCIÓN DE PRODUCTO: 'producto' llega ya resuelto y validado en
ctx.state['producto'] (lista canónica), escrito por el clasificador
unificado en tasks.py. Este handler nunca resuelve producto por su
cuenta ni lo lee de ctx.entidades_detectadas.

CICLO DE VIDA DE 'producto' EN EL STATE: la bandera 'es_aclaracion'
(True mientras el turno termina en una pregunta abierta, False cuando
se cierra con un resultado) es la única señal que aporta este handler
al ciclo de vida de producto. El clasificador del turno siguiente lee
el historial completo para inferir si el producto sigue vigente.
"""
import json
import logging
from datetime import datetime
from typing import Dict, List, Optional

from ..ghl import send_message_to_ghl, send_multiple_messages
from ..catalog_cache import catalog_cache
from ..prompts import load_prompt
from .catalogo import get_catalog_url_for_model
from .context import IntentContext, registrar

logger = logging.getLogger(__name__)

MAX_ACLARACIONES_PRODUCTO = 4  # resguardo técnico, no lógica de negocio


@registrar("busqueda_producto_modelo")
def handle(ctx: IntentContext) -> Optional[dict]:
    logger.info("🔍 Procesando consulta de catálogo — cliente define/cambia modelo en este mensaje")

    # ============================================
    # 1. LEER MODELO YA RESUELTO — nunca resolver acá
    #
    # El clasificador unificado (tasks.py) ya resolvió y validó el
    # modelo contra el catálogo antes de despachar a este handler.
    # Si es None, el clasificador no pudo identificarlo en este turno.
    # ============================================
    modelo = ctx.state.get('modelo')

    if not modelo:
        texto_modelo_llm = ctx.entidades_detectadas.get('modelo', '')
        logger.warning(
            f"⚠️ Modelo no resuelto por clasificador "
            f"(texto detectado: '{texto_modelo_llm}'), consultando LLM para aclaración/derivación"
        )
        decision = _decidir_aclaracion_o_derivacion_llm(ctx, texto_modelo_llm)
        accion = decision.get("accion")

        if accion == "derivar_humano":
            logger.info("⛔ LLM determinó que ya se pidió aclaración de modelo antes, derivando a agente humano")
            mensaje = decision.get("mensaje") or (
                "No logramos identificar el modelo del que hablas. "
                "Te voy a poner en contacto con un asesor para que te ayude directamente."
            )
            send_message_to_ghl(ctx.contact_id, mensaje, ctx.channel)
            return {
                "success": True,
                "response": mensaje,
                "contact_id": ctx.contact_id,
                "intencion": ctx.intencion,
                "fallback": True,
                "fallback_tipo": "modelo_no_resuelto_derivar_humano",
                "processed_at": datetime.now().isoformat(),
            }

        # accion == "pedir_aclaracion"
        mensaje = decision.get("mensaje") or (
            f"No reconozco el modelo o categoría \"{texto_modelo_llm}\". "
            "¿Podrías confirmarme el nombre exacto?"
        )
        send_message_to_ghl(ctx.contact_id, mensaje, ctx.channel)
        return {
            "success": True,
            "response": mensaje,
            "contact_id": ctx.contact_id,
            "intencion": ctx.intencion,
            "esperando_confirmacion_modelo": True,
            "processed_at": datetime.now().isoformat(),
        }

    # ============================================
    # 2. LEER PRODUCTO YA RESUELTO (si lo hay) — nunca resolver acá
    # ============================================
    productos_state = ctx.state.get('producto') or []
    producto_pedido = productos_state[0] if productos_state else None

    # ============================================
    # 3. OBTENER CATÁLOGO DEL MODELO (todo el catálogo, sin filtrar por término)
    # ============================================
    productos_modelo = catalog_cache.get_productos_por_modelo(modelo)

    if not productos_modelo:
        logger.warning(f"⚠️ Modelo '{modelo}' sin productos en catálogo")
        ctx.state_manager.update_state(ctx.contact_id, {'es_aclaracion': False, 'intentos_producto': 0})
        return _fallback_catalogo_modelo(ctx, modelo)

    # ============================================
    # 4. LLM DECIDE ACCIÓN + SELECCIÓN (unificado)
    # ============================================
    decision = _decidir_accion_llm(ctx, modelo, producto_pedido, productos_modelo)
    accion = decision.get("accion")

    if accion == "pedir_aclaracion":
        intentos_prod = ctx.state.get('intentos_producto', 0) + 1

        if intentos_prod > MAX_ACLARACIONES_PRODUCTO:
            logger.warning("⛔ Circuit breaker: demasiadas aclaraciones sin resolver, forzando fallback")
            ctx.state_manager.update_state(ctx.contact_id, {'es_aclaracion': False, 'intentos_producto': 0})
            return _fallback_catalogo_modelo(ctx, modelo)

        ctx.state_manager.update_state(ctx.contact_id, {
            'es_aclaracion': True,
            'intentos_producto': intentos_prod,
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
        ctx.state_manager.update_state(ctx.contact_id, {'es_aclaracion': False, 'intentos_producto': 0})
        return _fallback_catalogo_modelo(ctx, modelo, mensaje_intro=decision.get("mensaje_fallback"))

    # accion == "buscar_producto"
    ctx.state_manager.update_state(ctx.contact_id, {'es_aclaracion': False, 'intentos_producto': 0})
    ids_seleccionados = decision.get("ids_seleccionados", [])
    productos_resueltos = [p for p in productos_modelo if p.get('id') in ids_seleccionados]

    if not productos_resueltos:
        return _fallback_catalogo_modelo(ctx, modelo)

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
        if producto_agotado.get('url'):
            mensajes = [
                "Este producto está agotado por el momento. Puedes hacer clic en el producto "
                "para suscribirte y te avisamos apenas vuelva a estar disponible — "
                "*_esto no implica ninguna obligación de compra._*",
                f"{producto_agotado['url']}\n*_AGOTADO_*, Suscríbete para avisarte cuando llegue.",
            ]
        else:
            mensajes = []
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


def _decidir_aclaracion_o_derivacion_llm(ctx: IntentContext, texto_modelo_llm: str) -> Dict:
    """
    Decide si corresponde pedir aclaración del modelo (primera vez que
    no resuelve en el tramo reciente de conversación) o derivar a un
    agente humano (ya se pidió aclaración de modelo antes y el cliente
    volvió a dar algo que tampoco resolvió). Un humano haría esto
    recordando la conversación, no contando intentos — por eso esta
    decisión la toma un LLM leyendo el historial, en vez de un contador
    de estado.
    """
    system_prompt = f"""Eres el asistente de ventas de Quinchau (repuestos de motos).

El cliente mencionó un modelo o categoría que no reconocemos en nuestro
catálogo: "{texto_modelo_llm}".

HISTORIAL RECIENTE DE LA CONVERSACIÓN:
{ctx.historial_texto}

Tu tarea es decidir si esta es la primera vez que el modelo mencionado
por el cliente no se puede identificar, o si ya le pedimos aclaración
sobre el modelo antes en esta conversación y el cliente volvió a dar
un nombre que tampoco pudimos reconocer.

CRITERIOS:
- Si en el historial reciente NO hay un mensaje previo del asistente
  pidiendo que confirme o aclare el nombre del modelo: acción =
  "pedir_aclaracion". Redactá un mensaje breve y amable pidiéndole que
  confirme el nombre exacto del modelo o categoría.
- Si en el historial reciente el asistente YA le pidió aclarar el
  modelo, y el cliente respondió con otro nombre que tampoco se pudo
  identificar: acción = "derivar_humano". Redactá un mensaje breve
  informando que un asesor va a continuar la conversación para
  ayudarle a identificar el producto correcto.

RESPONDÉ ÚNICAMENTE CON ESTE JSON, SIN TEXTO ADICIONAL:
{{
    "accion": "pedir_aclaracion" | "derivar_humano",
    "mensaje": "...",
    "razon": "..."
}}"""

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
        logger.info(f"🎯 Resolución de modelo | Acción: {data.get('accion')} | Razón: {data.get('razon', '')}")
        return data

    except (json.JSONDecodeError, KeyError, IndexError) as e:
        logger.error(f"❌ Error parseando decisión de aclaración/derivación de modelo: {e}")
        return {
            "accion": "pedir_aclaracion",
            "mensaje": f"No reconozco el modelo o categoría \"{texto_modelo_llm}\". ¿Podrías confirmarme el nombre exacto?",
            "razon": "error_parseo",
        }


def _decidir_accion_llm(
    ctx: IntentContext,
    modelo: str,
    producto_pedido: Optional[str],
    productos_modelo: List[Dict],
) -> Dict:
    """
    Llamada única al LLM: decide la acción (buscar_producto /
    pedir_aclaracion / fallback_catalogo) razonando sobre el historial
    completo, y si corresponde, selecciona los ids que matchean lo
    pedido por el cliente.
    """
    productos_texto = "\n".join(
        f"- {p.get('id')}: {p.get('nombre', '')}"
        for p in productos_modelo
        if p.get('id')
    )

    system_prompt = load_prompt(
        "prompt_seleccion_catalogo",
        nombre_cliente=ctx.first_name,
        producto=producto_pedido or '',
        modelo=modelo,
        intencion=ctx.intencion,
        historial_texto=ctx.historial_texto,
        productos_texto=productos_texto,
        intentos_previos=ctx.state.get('intentos_producto', 0),
    )

    logger.info(
        f"📝 Prompt decisión de catálogo armado | modelo={modelo} | "
    f"productos_en_catalogo={len(productos_modelo)} | "
    f"producto_pedido={producto_pedido!r} | "
    f"intentos_previos={ctx.state.get('intentos_producto', 0)}"
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

        logger.info(
            f"🎯 Acción: {data.get('accion')} | "
            f"es_aclaracion: {data['es_aclaracion']} | "
            f"Ids: {ids} | Razón: {data.get('razon', '')}"
        )
        return data

    except (json.JSONDecodeError, KeyError, IndexError) as e:
        logger.error(f"❌ Error parseando respuesta de decisión de catálogo: {e}")
        return {"accion": "fallback_catalogo", "ids_seleccionados": [], "es_aclaracion": False, "razon": "error_parseo"}


def _fallback_catalogo_modelo(
    ctx: IntentContext,
    modelo: str,
    mensaje_intro: Optional[str] = None,
) -> dict:
    """Respuesta final del turno cuando no hay match posible. Sin reintentos."""
    catalog_info = get_catalog_url_for_model(modelo)

    if catalog_info and catalog_info.get('found'):
        catalogo_url = catalog_info.get('url')
        modeldescrip = catalog_info.get('modeldescrip', modelo)
        intro = mensaje_intro or (
            f"No encontré ese producto específico para {modeldescrip}. "
            f"Te invito a revisar el catálogo completo:"
        )
        mensajes = [intro, catalogo_url]
    else:
        catalogo_url = None
        intro = mensaje_intro or (
            f"No encontré ese producto para {modelo}. Te invito a revisar el catálogo general:"
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
        "modelo_contexto": modelo,
        "catalogo_url": catalogo_url,
        "processed_at": datetime.now().isoformat(),
    }