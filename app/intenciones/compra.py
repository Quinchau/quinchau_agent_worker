import logging
from datetime import datetime
from typing import List, Optional

from ..ghl import send_message_to_ghl, send_multiple_messages
from ..prompts import load_prompt
from .catalogo import get_catalog_url_for_model, resolver_y_responder_catalogo
from .context import IntentContext, registrar

logger = logging.getLogger(__name__)


@registrar("intencion_compra")
def handle(ctx: IntentContext) -> Optional[dict]:
    """
    Maneja consultas de compra/disponibilidad/precio.
    
    NUEVO FLUJO: Usa flags de resolución del EntityResolver.
    NO usa IntentClassifier.
    """
    logger.info("🔍 Procesando consulta de catálogo (compra/disponibilidad/precio)")
    
    # ============================================
    # OBTENER ESTADO (FUENTE DE VERDAD)
    # ============================================
    producto = ctx.state.get('producto')
    modelo = ctx.state.get('modelo')
    product_found = ctx.state.get('product_found', False)
    model_found = ctx.state.get('model_found', False)
    estado_resolucion = ctx.state.get('estado_resolucion', 'no_encontrado')
    entidades_no_resueltas = ctx.state.get('entidades_no_resueltas', [])
    intentos_resolucion = ctx.state.get('intentos_resolucion', 0)
    
    logger.info(f"📊 Estado resolución: {estado_resolucion}")
    logger.info(f"   producto: {producto} (found={product_found})")
    logger.info(f"   modelo: {modelo} (found={model_found})")
    logger.info(f"   entidades_no_resueltas: {entidades_no_resueltas}")
    logger.info(f"   intentos: {intentos_resolucion}")
    
    # ============================================
    # CASO 1: TRUE, TRUE → BACKEND
    # ============================================
    if product_found and model_found:
        logger.info(f"✅ Entidades resueltas: producto='{producto}', modelo='{modelo}'")
        
        # Limpiar flags de resolución
        _limpiar_flags_resolucion(ctx)
        
        # Consultar backend
        catalog_result = resolver_y_responder_catalogo(
            ctx.state, 
            ctx.contact_id, 
            ctx.intencion, 
            ctx.channel,
            historial_texto=ctx.historial_texto,
            mensaje_actual=ctx.message
        )
        
        if catalog_result:
            logger.info("📤 Respuesta del backend enviada")
            return catalog_result
        
        # Si el catálogo falló, usar fallback
        logger.warning("⚠️ Catálogo falló, usando fallback")
        return _fallback_limite_intentos(ctx)
    
    # ============================================
    # CASO 2: ENTIDADES FALTANTES
    # ============================================
    if not product_found or not model_found:
        logger.info(f"⚠️ Faltan entidades: {entidades_no_resueltas}")
        
        intentos_resolucion += 1
        ctx.state_manager.update_state(ctx.contact_id, {
            "intentos_resolucion": intentos_resolucion
        })
        
        if intentos_resolucion >= 3:
            logger.warning("🚨 Límite de 3 intentos alcanzado")
            return _fallback_limite_intentos(ctx)
        
        return _manejar_entidades_faltantes(
            ctx, 
            entidades_no_resueltas,
            producto,
            modelo,
            intentos_resolucion
        )
    
    # ============================================
    # 3. FALLBACK: Estado inesperado
    # ============================================
    logger.warning(f"⚠️ Estado inesperado: {estado_resolucion}. Usando fallback.")
    return _fallback_limite_intentos(ctx)


def _manejar_entidades_faltantes(
    ctx: IntentContext,
    faltantes: List[str],
    producto: Optional[str],
    modelo: Optional[str],
    intentos_resolucion: int
) -> dict:
    """
    Maneja casos donde faltan entidades (no encontradas en catálogo).
    """
    logger.info(f"⚠️ Faltan entidades: {faltantes}")
    logger.info(f"ℹ️ Intento {intentos_resolucion} de 3. Generando pregunta.")
    
    # Actualizar estado con el contador
    ctx.state_manager.update_state(ctx.contact_id, {
        "intentos_resolucion": intentos_resolucion,
        "entidades_no_resueltas": faltantes,
    })
    
    # Construir contexto para el prompt
    if 'producto' in faltantes and modelo:
        contexto_faltantes = f"El cliente tiene modelo '{modelo}' pero falta determinar el producto."
    elif 'modelo' in faltantes and producto:
        contexto_faltantes = f"El cliente tiene producto '{producto}' pero falta determinar el modelo."
    elif 'producto' in faltantes and not modelo:
        contexto_faltantes = "Falta determinar el producto. El cliente no tiene modelo previo."
    elif 'modelo' in faltantes and not producto:
        contexto_faltantes = "Falta determinar el modelo. El cliente no tiene producto previo."
    else:
        contexto_faltantes = "Faltan determinar producto y modelo."
    
    # Obtener última pregunta para no repetir
    ultima_pregunta = ctx.state.get('ultima_pregunta_entidades', '') or ''
    
    # ============================================
    # GENERAR PREGUNTA CON HISTORIAL COMPLETO
    # ============================================
    logger.info(f"📋 Historial en ctx: {len(ctx.historial_texto)} caracteres")
    if ctx.historial_texto:
        logger.info(f"📋 Primeros 100 caracteres del historial: {ctx.historial_texto[:100]}...")
    
    prompt_pregunta = load_prompt(
        "prompt_entidades_faltantes",
        first_name=ctx.first_name,
        last_name=ctx.last_name,
        contexto_faltantes=contexto_faltantes,
        message=ctx.message,
        intencion=ctx.intencion,
        faltantes=faltantes,
        intentos=intentos_resolucion,
        ultima_pregunta=ultima_pregunta,
        historial_texto=ctx.historial_texto,  # ← HISTORIAL COMPLETO
    )
    
    logger.info("📝 PROMPT ENVIADO A OPENAI (Entidades Faltantes):")
    logger.info("-" * 50)
    logger.info(prompt_pregunta)
    logger.info("-" * 50)
    
    try:
        pregunta = ctx.client.chat.completions.create(
            model="openai/gpt-4o-mini",
            messages=[{"role": "user", "content": prompt_pregunta}],
            temperature=0.5,
            max_tokens=80,
        ).choices[0].message.content.strip()
    except Exception as e:
        logger.error(f"❌ Error generando pregunta: {e}")
        # Fallback a pregunta genérica
        if not modelo and producto:
            pregunta = f"No encontré el modelo para '{producto}'. ¿Podrías confirmar el modelo de tu moto?"
        elif not producto and modelo:
            pregunta = f"No encontré el producto para '{modelo}'. ¿Podrías describir mejor la pieza que necesitas?"
        else:
            pregunta = "No logré identificar el producto o modelo. ¿Podrías describir mejor lo que necesitas?"
    
    # Guardar estado
    ctx.state_manager.update_state(ctx.contact_id, {
        "entidades_no_resueltas": faltantes,
        "esperando_confirmacion": True,
        "esperando_respuesta": True,
        "ultima_pregunta_entidades": pregunta,
        "intentos_resolucion": intentos_resolucion,
    })
    
    # Enviar mensaje
    send_message_to_ghl(ctx.contact_id, pregunta, ctx.channel)
    logger.info(f"📤 Pregunta generada: {pregunta}")
    
    return {
        "success": True,
        "response": pregunta,
        "contact_id": ctx.contact_id,
        "intencion": ctx.intencion,
        "entidades_faltantes": faltantes,
        "generado_por": "llm",
        "intentos": intentos_resolucion,
        "processed_at": datetime.now().isoformat(),
    }


def _fallback_limite_intentos(ctx: IntentContext) -> dict:
    """
    Fallback cuando se alcanza el límite de 3 intentos.
    """
    logger.warning("🚨 Límite de 3 intentos alcanzado. Activando fallback.")
    
    # ============================================
    # LOG DEL HISTORIAL PARA CONTEXTO
    # ============================================
    if ctx.historial_texto:
        logger.info(f"📋 Historial disponible para fallback: {len(ctx.historial_texto)} caracteres")
    else:
        logger.info("📋 Sin historial disponible para fallback")
    
    modelo = ctx.state.get('modelo')
    producto = ctx.state.get('producto')
    product_found = ctx.state.get('product_found', False)
    model_found = ctx.state.get('model_found', False)
    catalogo_url = None
    
    # ============================================
    # CASO: Solo modelo encontrado → Catálogo del modelo
    # ============================================
    if model_found and not product_found:
        catalog_info = get_catalog_url_for_model(modelo)
        
        if catalog_info and catalog_info.get('found'):
            catalogo_url = catalog_info.get('url')
            modeldescrip = catalog_info.get('modeldescrip', modelo)
            
            mensajes = [
                f"📋 No encontré el producto específico, pero aquí tienes el catálogo completo de {modeldescrip}:",
                catalogo_url,
            ]
            logger.info(f"📦 URL del catálogo obtenida del backend: {catalogo_url}")
        else:
            mensajes = [
                f"📋 No encontré el producto para {modelo}. "
                f"Te invito a revisar el catálogo general:",
                "https://quinchau.com/repuestos-motos",
            ]
        
        send_multiple_messages(ctx.contact_id, mensajes, ctx.channel, delay=0.5)
        response_data = mensajes
        
    # ============================================
    # CASO: Solo producto encontrado → Preguntar modelo
    # ============================================
    elif product_found and not model_found:
        mensaje = (
            f"📦 Encontré '{producto}' pero no pude identificar el modelo de tu moto. "
            f"¿Podrías confirmar el modelo para mostrarte la información correcta?"
        )
        send_message_to_ghl(ctx.contact_id, mensaje, ctx.channel)
        response_data = mensaje
        
        # Marcar para esperar respuesta
        ctx.state_manager.update_state(ctx.contact_id, {
            "esperando_confirmacion": True,
            "esperando_respuesta": True,
            "entidades_no_resueltas": ["modelo"],
        })
        
        ctx.state_manager.update_state(ctx.contact_id, {
            "intentos_resolucion": 0,
            "fallback_activado": True,
            "ultimo_fallback": datetime.now().isoformat(),
        })
        
        return {
            "success": True,
            "response": mensaje,
            "contact_id": ctx.contact_id,
            "intencion": ctx.intencion,
            "fallback": True,
            "fallback_tipo": "solo_producto",
            "producto_contexto": producto,
            "processed_at": datetime.now().isoformat(),
        }
    
    # ============================================
    # CASO: Nada encontrado → Catálogo general
    # ============================================
    else:
        mensaje = (
            "📋 No logré identificar ni el producto ni el modelo que buscas. "
            "Te invito a revisar nuestro catálogo general en: https://quinchau.com"
        )
        send_message_to_ghl(ctx.contact_id, mensaje, ctx.channel)
        response_data = mensaje
    
    # ============================================
    # Limpiar estado y finalizar
    # ============================================
    ctx.state_manager.update_state(ctx.contact_id, {
        "intentos_resolucion": 0,
        "entidades_no_resueltas": [],
        "fallback_activado": True,
        "ultimo_fallback": datetime.now().isoformat(),
        "ultima_pregunta_entidades": None,
        "esperando_confirmacion": False,
        "esperando_respuesta": False,
    })
    
    logger.info(f"📤 Fallback enviado para {ctx.contact_id}")
    
    return {
        "success": True,
        "response": response_data,
        "contact_id": ctx.contact_id,
        "intencion": ctx.intencion,
        "fallback": True,
        "fallback_tipo": _determinar_fallback_tipo(product_found, model_found),
        "modelo_contexto": modelo,
        "producto_contexto": producto,
        "catalogo_url": catalogo_url,
        "processed_at": datetime.now().isoformat(),
    }


def _limpiar_flags_resolucion(ctx: IntentContext) -> None:
    """Limpia todos los flags de resolución del state."""
    ctx.state_manager.update_state(ctx.contact_id, {
        "intentos_resolucion": 0,
        "entidades_no_resueltas": [],
        "product_found": False,
        "model_found": False,
        "estado_resolucion": "resuelto",
        "esperando_confirmacion": False,
        "esperando_respuesta": False,
        "ultima_pregunta_entidades": None,
        "fallback_activado": False,
    })


def _determinar_fallback_tipo(product_found: bool, model_found: bool) -> str:
    """Determina el tipo de fallback según lo encontrado."""
    if product_found and model_found:
        return "ambos_encontrados"
    elif product_found and not model_found:
        return "solo_producto"
    elif not product_found and model_found:
        return "solo_modelo"
    else:
        return "nada_encontrado"