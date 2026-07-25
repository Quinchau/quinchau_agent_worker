"""
Intención: informacion_general

Cliente pregunta sobre logística u operación del negocio (envíos, pagos,
horarios, ubicación, garantías), con o sin mención de un producto ya
identificado. Se busca la FAQ correspondiente y, si hay match de alta
confianza, se redacta una respuesta con esa información. Si no hay
match, se deriva a revisión humana sin inventar información.
"""
import json
import logging
from datetime import datetime

from ..ghl import send_message_to_ghl
from ..prompts import load_prompt
from ..catalog_cache import catalog_cache
from .context import IntentContext, registrar

logger = logging.getLogger(__name__)

CONFIANZA_MINIMA_AUTORESPUESTA = "alta"


@registrar("informacion_general")
def handle(ctx: IntentContext) -> dict:
    logger.info(f"ℹ️ Consulta de información general - {ctx.first_name} {ctx.last_name}")

    faqs = catalog_cache.get_system_faqs()

    if faqs:
        resultado = _intentar_resolver_con_faq(ctx, faqs)
        if resultado is not None:
            return resultado

    return _derivar_sin_informacion(ctx)


def _intentar_resolver_con_faq(ctx: IntentContext, faqs: list) -> dict | None:
    seleccion = _seleccionar_faq(ctx, faqs)

    faq_id_raw = seleccion.get("faq_id", "ninguna")
    confianza = seleccion.get("confianza", "baja")
    razon = seleccion.get("razon", "")

    logger.info(f"🎯 Selector: faq_id={faq_id_raw} | confianza={confianza} | razon={razon}")

    if faq_id_raw != "ninguna" and confianza == CONFIANZA_MINIMA_AUTORESPUESTA:
        faq_id_num = int(str(faq_id_raw).replace("faq_", ""))
        faq = next((f for f in faqs if f["id"] == faq_id_num), None)

        if not faq:
            logger.warning(f"⚠️ faq_{faq_id_num} no encontrada en listado cacheado, derivando a fallback")
            _log_faq_interaction(
                ctx, faq_id=faq_id_num, confianza=confianza, razon="faq_no_encontrada_en_cache",
                respuesta_final=None, resultado="derivado_humano",
            )
            return None

        respuesta = _redactar_respuesta_faq(ctx, faq["answer"])

        logger.info(f"✅ Respuesta redactada desde faq_{faq_id_num}: {respuesta[:40]}...")
        send_message_to_ghl(ctx.contact_id, respuesta, ctx.channel)

        ctx.state_manager.update_state(ctx.contact_id, {
            'ultima_intencion': ctx.intencion,
            'ultimo_faq_resuelto': faq_id_num,
        })

        _log_faq_interaction(
            ctx, faq_id=faq_id_num, confianza=confianza, razon=razon,
            respuesta_final=respuesta, resultado="resuelto",
        )

        return {
            "success": True,
            "response": respuesta,
            "contact_id": ctx.contact_id,
            "intencion": ctx.intencion,
            "status": "active",
            "processed_at": datetime.now().isoformat(),
        }

    _log_faq_interaction(
        ctx, faq_id=None, confianza=confianza, razon=razon,
        respuesta_final=None, resultado="derivado_humano",
    )
    return None


def _seleccionar_faq(ctx: IntentContext, faqs: list) -> dict:
    listado = "\n".join(f"- faq_{f['id']}: {f['question']}" for f in faqs)

    system_prompt = load_prompt(
        "prompt_faq_selector_general",
        first_name=ctx.first_name,
        historial_texto=ctx.historial_texto,
        listado_faqs=listado,
        mensaje=ctx.message,
    )

    try:
        llm_response = ctx.client.chat.completions.create(
            model="openai/gpt-4o-mini",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": f"El cliente dice: \"{ctx.message}\""},
            ],
            temperature=0.1,
            max_tokens=120,
        )
        contenido = llm_response.choices[0].message.content.strip()
        return json.loads(contenido)
    except (json.JSONDecodeError, Exception) as e:
        logger.warning(f"⚠️ Selector FAQ falló o devolvió JSON inválido: {e}")
        return {"faq_id": "ninguna", "confianza": "baja", "razon": "error_selector"}


def _redactar_respuesta_faq(ctx: IntentContext, faq_answer: str) -> str:
    system_prompt = load_prompt(
        "prompt_faq_redactor",
        first_name=ctx.first_name,
        historial_texto=ctx.historial_texto,
        contenido_referencia=faq_answer,
    )

    try:
        llm_response = ctx.client.chat.completions.create(
            model="openai/gpt-4o-mini",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": f"El cliente dice: \"{ctx.message}\""},
            ],
            temperature=0.3,
            max_tokens=100,
        )
        return llm_response.choices[0].message.content.strip()
    except Exception as e:
        logger.error(f"❌ Error redactando respuesta FAQ: {e}")
        return faq_answer  # fallback: contenido crudo antes que fallar el envío


def _log_faq_interaction(ctx: IntentContext, faq_id, confianza, razon, respuesta_final, resultado):
    from ..database import get_db_connection

    query = """
        INSERT INTO faq_interactions
            (contact_id, mensaje, faq_id_seleccionado, confianza, razon_seleccion, respuesta_final, resultado)
        VALUES
            (%s, %s, %s, %s, %s, %s, %s)
    """
    params = (
        ctx.contact_id,
        ctx.message,
        faq_id,
        confianza,
        razon,
        respuesta_final,
        resultado,
    )

    conn = None
    try:
        conn = get_db_connection()
        with conn.cursor() as cursor:
            cursor.execute(query, params)
        conn.commit()
        logger.info(f"📝 faq_interactions registrado: resultado={resultado}, faq_id={faq_id}")
    except Exception as e:
        logger.error(f"❌ Error insertando faq_interactions: {e}")
    finally:
        if conn:
            conn.close()


def _derivar_sin_informacion(ctx: IntentContext) -> dict:
    respuesta = (
        f"Dejame confirmar esa información con el equipo, {ctx.first_name}, "
        "y te respondo apenas la tenga."
    )

    send_message_to_ghl(ctx.contact_id, respuesta, ctx.channel)

    ctx.state_manager.update_state(ctx.contact_id, {
        'ultima_intencion': ctx.intencion,
        'requiere_revision_humana': True,
        'ultimo_mensaje_no_clasificado': ctx.message,
        'timestamp_no_clasificado': datetime.now().isoformat(),
    })

    return {
        "success": True,
        "response": respuesta,
        "contact_id": ctx.contact_id,
        "intencion": ctx.intencion,
        "status": "active",
        "processed_at": datetime.now().isoformat(),
    }