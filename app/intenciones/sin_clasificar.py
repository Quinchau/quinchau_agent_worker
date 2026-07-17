"""
Intención: sin_clasificar

El clasificador no pudo determinar qué quiere el cliente. Primero se evalúa
si el mensaje corresponde a una FAQ general del negocio (system_faqs); si
hay match de alta confianza, se redacta una respuesta con esa información y
se resuelve sin intervención humana. Si no hay match, se genera una
respuesta natural vía LLM y se marca la interacción para revisión humana
asíncrona (comportamiento original, sin cambios).
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


@registrar("sin_clasificar")
def handle(ctx: IntentContext) -> dict:
    logger.info(f"🤔 Intención sin clasificar → evaluando FAQs - {ctx.first_name} {ctx.last_name}")

    faqs = catalog_cache.get_system_faqs()

    if faqs:
        resultado_faq = _intentar_resolver_con_faq(ctx, faqs)
        if resultado_faq is not None:
            return resultado_faq

    # ------------------------------------------------------------
    # Comportamiento original (sin match de FAQ, o sin FAQs activas)
    # ------------------------------------------------------------
    return _handle_fallback_generico(ctx)


def _intentar_resolver_con_faq(ctx: IntentContext, faqs: list) -> dict | None:
    """
    Evalúa el mensaje contra el selector (FAQ / social / queja / ninguna).
    Resuelve directamente los casos social y queja (respuesta redactada en
    el mismo llamado). Para FAQ de alta confianza, redacta la respuesta en
    un segundo paso usando el contenido real de la FAQ.
    Retorna None si no se pudo resolver (para que el caller siga al fallback
    genérico).
    """
    seleccion = _seleccionar_faq(ctx, faqs)

    tipo = seleccion.get("tipo", "ninguna")
    faq_id_raw = seleccion.get("faq_id", "ninguna")
    respuesta_directa = (seleccion.get("respuesta_directa") or "").strip()
    confianza = seleccion.get("confianza", "baja")
    razon = seleccion.get("razon", "")

    logger.info(f"🎯 Selector: tipo={tipo} | faq_id={faq_id_raw} | confianza={confianza} | razon={razon}")

    # ------------------------------------------------------------
    # Camino corto: social o queja → respuesta directa, sin segunda llamada
    # ------------------------------------------------------------
    if tipo in ("social", "queja") and respuesta_directa:
        resultado_log = "resuelto_social" if tipo == "social" else "escalado_queja"

        send_message_to_ghl(ctx.contact_id, respuesta_directa, ctx.channel)

        state_update = {'ultima_intencion': ctx.intencion}
        if tipo == "queja":
            state_update.update({
                'requiere_revision_humana': True,
                'motivo_revision': 'queja',
                'ultimo_mensaje_no_clasificado': ctx.message,
                'timestamp_no_clasificado': datetime.now().isoformat(),
            })
        ctx.state_manager.update_state(ctx.contact_id, state_update)

        _log_faq_interaction(
            ctx, faq_id=None, confianza=confianza, razon=razon,
            respuesta_final=respuesta_directa, resultado=resultado_log,
        )

        logger.info(f"✅ Respuesta directa ({tipo}) enviada: {respuesta_directa[:40]}...")

        return {
            "success": True,
            "response": respuesta_directa,
            "contact_id": ctx.contact_id,
            "intencion": ctx.intencion,
            "status": "active",
            "processed_at": datetime.now().isoformat(),
        }

    # ------------------------------------------------------------
    # Camino FAQ: requiere segunda llamada para redactar con el
    # contenido real (faq["answer"]) que no está en el prompt selector
    # ------------------------------------------------------------
    if tipo == "faq" and faq_id_raw != "ninguna" and confianza == CONFIANZA_MINIMA_AUTORESPUESTA:
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

    # ------------------------------------------------------------
    # ninguna, baja/media confianza, o campos incompletos → fallback genérico
    # ------------------------------------------------------------
    _log_faq_interaction(
        ctx, faq_id=None, confianza=confianza, razon=razon,
        respuesta_final=None, resultado="derivado_humano",
    )
    return None


def _seleccionar_faq(ctx: IntentContext, faqs: list) -> dict:
    listado = "\n".join(f"- faq_{f['id']}: {f['question']}" for f in faqs)

    system_prompt = load_prompt(
        "prompt_faq_selector",
        first_name=ctx.first_name,
        historial_texto=ctx.historial_texto,
        listado_faqs=listado,
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


def _handle_fallback_generico(ctx: IntentContext) -> dict:
    logger.info(f"🤔 Sin match de FAQ → generando respuesta natural - {ctx.first_name} {ctx.last_name}")

    system_prompt = load_prompt(
        "prompt_sin_clasificar",
        first_name=ctx.first_name,
        historial_texto=ctx.historial_texto,
    )

    try:
        llm_response = ctx.client.chat.completions.create(
            model="openai/gpt-4o-mini",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": f"El cliente {ctx.first_name} dice: \"{ctx.message}\""},
            ],
            temperature=0.3,
            max_tokens=80,
        )
        respuesta = llm_response.choices[0].message.content.strip()
    except Exception as e:
        logger.error(f"❌ Error generando respuesta sin_clasificar: {e}")
        respuesta = (
            f"No estoy seguro de haber entendido, {ctx.first_name}. "
            "¿Podrías contarme qué repuesto o modelo necesitas?"
        )

    logger.info(f"✅ Respuesta sin_clasificar enviada: {respuesta[:40]}...")

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