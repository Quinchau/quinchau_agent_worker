"""
Intención: sin_clasificar

El clasificador no pudo determinar qué quiere el cliente, o el mensaje es
ruido, un comentario social (saludo/agradecimiento suelto) o una queja.
Se evalúa el mensaje contra el selector para detectar esos casos puntuales
(social/queja tienen respuesta directa); si no aplica ninguno, se genera
una respuesta natural vía LLM y se marca la interacción para revisión
humana asíncrona.

Nota: la resolución de FAQs de información general (envíos, pagos,
horarios, ubicación, garantías) ya no vive acá — es responsabilidad de
informacion_general.
"""
import json
import logging
from datetime import datetime

from ..ghl import send_message_to_ghl
from ..prompts import load_prompt
from ..catalog_cache import catalog_cache
from .context import IntentContext, registrar

logger = logging.getLogger(__name__)


@registrar("sin_clasificar")
def handle(ctx: IntentContext) -> dict:
    logger.info(f"🤔 Intención sin clasificar - {ctx.first_name} {ctx.last_name}")

    faqs = catalog_cache.get_system_faqs()

    if faqs:
        resultado = _intentar_resolver_social_o_queja(ctx, faqs)
        if resultado is not None:
            return resultado

    # ------------------------------------------------------------
    # Comportamiento original (sin match de social/queja)
    # ------------------------------------------------------------
    return _handle_fallback_generico(ctx)


def _intentar_resolver_social_o_queja(ctx: IntentContext, faqs: list) -> dict | None:
    """
    Evalúa el mensaje contra el selector. Solo resuelve acá los casos
    social y queja (respuesta directa); cualquier otro resultado (faq,
    ninguna) devuelve None para que el caller siga al fallback genérico
    — el caso "faq" ya no debería llegar acá si el clasificador de nivel
    1 enruta bien, pero si llegara, tampoco lo resolvemos en este módulo.
    """
    seleccion = _seleccionar_faq(ctx, faqs)

    tipo = seleccion.get("tipo", "ninguna")
    respuesta_directa = (seleccion.get("respuesta_directa") or "").strip()
    confianza = seleccion.get("confianza", "baja")
    razon = seleccion.get("razon", "")

    logger.info(f"🎯 Selector: tipo={tipo} | confianza={confianza} | razon={razon}")

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
    logger.info(f"🤔 Sin match social/queja → generando respuesta natural - {ctx.first_name} {ctx.last_name}")

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