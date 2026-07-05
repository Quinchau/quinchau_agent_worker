"""
Intención: intencion_compra_al_mayoreo
Handler que detecta compras al por mayor y genera respuestas naturales
llamando directamente al LLM (mismo patrón que saludo.py y compra.py).
"""
import logging
from datetime import datetime

from ..ghl import send_message_to_ghl
from ..prompts import load_prompt
from .context import IntentContext, registrar

logger = logging.getLogger(__name__)

PROMPT_NOMBRE = "prompt_intencion_compra_al_mayoreo"


@registrar("intencion_compra_al_mayoreo")
def handle(ctx: IntentContext) -> dict:
    """
    Handler para compras al mayoreo. Llama al LLM directamente
    y envía la respuesta, sin pasar por el flujo de inyección de tasks.py.
    """
    # ============================================================
    # 1. VALIDACIONES
    # ============================================================
    if not ctx.contact_id:
        logger.error("❌ contact_id no presente en el contexto")
        return {
            "success": False,
            "error": "contact_id requerido",
            "intencion": ctx.intencion
        }

    user_name = ctx.first_name or "cliente"
    mensaje_usuario = ctx.message or ""

    # ============================================================
    # 2. CONTEXTO DE PRODUCTO/MODELO DESDE EL ESTADO
    # ============================================================
    estado = ctx.state or {}
    producto = estado.get('producto', '')
    modelo = estado.get('modelo', '')
    ultima_respuesta = estado.get('ultima_respuesta', '')

    product_context = ""
    if producto:
        product_context += f"Producto consultado: {producto}\n"
    if modelo:
        product_context += f"Modelo: {modelo}\n"
    if ultima_respuesta:
        product_context += f"Última respuesta del asistente: {ultima_respuesta[:200]}...\n"

    # ============================================================
    # 3. CONTROL DE REPETICIÓN
    # ============================================================
    veces_inyectado = estado.get('veces_inyectado_mayoreo', 0)

    nota_repeticion = ""
    if veces_inyectado >= 1:
        nota_repeticion = (
            f"\nNOTA: El cliente ya recibió información de mayoreo {veces_inyectado} "
            f"vez(es) antes en esta conversación. No repitas la misma información ni los "
            f"mismos enlaces de la misma forma. Preguntale si ya revisó los listados, u "
            f"ofrecele ayuda específica con algún producto."
        )

    # ============================================================
    # 4. CARGAR PROMPT
    # ============================================================
    try:
        system_prompt = load_prompt(
            PROMPT_NOMBRE,
            first_name=user_name,
            historial_texto=ctx.historial_texto or "",
            product_context=product_context or "No hay información de producto previa",
        ) + nota_repeticion
    except Exception as e:
        logger.error(f"❌ Error cargando prompt: {e}")
        system_prompt = (
            f"Sos Quinchau Assistant. Respondé de forma breve y natural a {user_name} "
            f"sobre compras al por mayor. Enlaces: descargar listados en "
            f"https://quinchau.com/downloader, ofertas en https://quinchau.com/ofertas."
            + nota_repeticion
        )

    # ============================================================
    # 5. LLAMAR AL LLM
    # ============================================================
    try:
        llm_response = ctx.client.chat.completions.create(
            model="openai/gpt-4o-mini",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": f"El cliente {user_name} dice: \"{mensaje_usuario}\""},
            ],
            temperature=0.5,
            max_tokens=200,
        )
        respuesta = llm_response.choices[0].message.content.strip()
    except Exception as e:
        logger.error(f"❌ Error generando respuesta de mayoreo: {e}")
        respuesta = (
            f"¡Hola {user_name}! Para compras al por mayor podés descargar nuestros "
            f"listados en https://quinchau.com/downloader y revisar las ofertas en "
            f"https://quinchau.com/ofertas. ¿Necesitás ayuda con algún producto en particular?"
        )

    # ============================================================
    # 6. ENVIAR RESPUESTA
    # ============================================================
    send_message_to_ghl(ctx.contact_id, respuesta, ctx.channel)
   
    # ============================================================
    # 7. ACTUALIZAR ESTADO
    # ============================================================
    try:
        ctx.state_manager.update_state(ctx.contact_id, {
            'ultima_intencion': ctx.intencion,
            'ultima_respuesta': respuesta,
            'contexto_mayoreo_inyectado': True,
            'veces_inyectado_mayoreo': veces_inyectado + 1,
            'fecha_ultima_inyeccion': datetime.now().isoformat(),
            'ultimo_mensaje_usuario': mensaje_usuario,
            'enlaces_mayoreo': {
                'downloader': 'https://quinchau.com/downloader',
                'ofertas': 'https://quinchau.com/ofertas'
            }
        })
    except Exception as e:
        logger.error(f"❌ Error actualizando estado: {e}")

    logger.info(f"✅ Respuesta de mayoreo enviada: {respuesta[:60]}...")

    return {
        "success": True,
        "response": respuesta,
        "contact_id": ctx.contact_id,
        "intencion": ctx.intencion,
        "veces_inyectado": veces_inyectado + 1,
        "processed_at": datetime.now().isoformat()
    }