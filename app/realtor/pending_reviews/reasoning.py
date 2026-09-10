import json
import logging
import os
import re
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo

from app.llm_client import get_openrouter_client

logger = logging.getLogger(__name__)

PROMPT_PATH = Path(__file__).resolve().parents[2] / "contextos" / "realtor" / "prompt_pending_reviews.txt"

# ==========================================
# ZONA HORARIA Y FECHA DE HOY (para inyectar en el prompt)
# ==========================================
try:
    MIAMI_TZ = ZoneInfo("America/New_York")
except Exception:
    from datetime import timezone, timedelta
    MIAMI_TZ = timezone(timedelta(hours=-4))

MESES_ABREV_ES = {
    1: "ENE", 2: "FEB", 3: "MAR", 4: "ABR", 5: "MAY", 6: "JUN",
    7: "JUL", 8: "AGO", 9: "SEP", 10: "OCT", 11: "NOV", 12: "DIC",
}

DIAS_SEMANA_ES = {
    0: "LUNES", 1: "MARTES", 2: "MIÉRCOLES", 3: "JUEVES",
    4: "VIERNES", 5: "SÁBADO", 6: "DOMINGO",
}


def _formatear_fecha_hoy(dt: datetime) -> str:
    """Ej: 'JUEVES 08 SEP 2026' — se inyecta en el prompt como única fuente de verdad de la fecha actual."""
    return f"{DIAS_SEMANA_ES[dt.weekday()]} {dt.day:02d} {MESES_ABREV_ES[dt.month]} {dt.year}"


NOTA_BITACORA_SCHEMA = {
    "type": "string",
    "description": (
        "Cuerpo de la bitácora para el historial del contacto en GHL: un resumen "
        "cronológico de TODA la conversación hasta ahora (no solo del último "
        "mensaje), regenerado completo en cada análisis. Un renglón por hito "
        "relevante (inicio de conversación, envío de información, falta de "
        "respuesta, pregunta de precio, etc.), con el formato exacto "
        "'DD MES_ABREV_MAYUS YYYY <resumen breve del hito>' (ej: "
        "'25 JUL 2026 Jaime inicia conversación preguntando por el proyecto de "
        "casas en Homestead, el agente respondió enviando la información'). Las "
        "fechas deben salir de los timestamps reales presentes en el historial, "
        "NUNCA inventadas. NO incluyas encabezado ni la fecha de hoy (eso se "
        "agrega automáticamente en el sistema). NO repitas el 'razonamiento' "
        "palabra por palabra ni uses términos internos del sistema (temperatura, "
        "calificación, nombres de herramientas, etc.)."
    ),
}

TEMPERATURA_SCHEMA = {
    "type": "string",
    "enum": ["Caliente", "Tibio", "Frio", "No interesado"],
    "description": (
        "Temperatura del lead según su intención de compra más reciente: "
        "Caliente (listo para actuar), Tibio (interesado sin urgencia), "
        "Frio (baja intención o inactivo), No interesado (rechazo explícito "
        "o descarte)."
    ),
}

RAZONAMIENTO_SCHEMA = {
    "type": "string",
    "description": (
        "Análisis MUY CONCISO (máx. 3-4 oraciones). Debe explicar obligatoriamente: "
        "1) Qué se solicitó explícitamente (menciona si se usaron palabras clave como 'crear', 'nueva' o 'agendar desde cero'). "
        "2) Si existe una tarea abierta con el EXACTO MISMO PROPÓSITO (menciona su ID y título). "
        "3) Por qué se eligió crear o actualizar, basándose en si fue una solicitud nueva o una simple corrección de fecha de algo ya pendiente."
    ),
}

DIAS_PARA_VENCER_SCHEMA = {
    "type": "integer",
    "minimum": 0,
    "maximum": 14,
    "description": (
        "Cuántos días a partir de HOY debe ejecutarse esta tarea, según la "
        "urgencia real detectada en la conversación. NO es una fecha, es un "
        "número de días de distancia: 0 = hoy mismo. 1 = mañana. Usa 0-1 para "
        "seguimientos urgentes o normales de corto plazo. Usa un número mayor "
        "SOLO si el cliente pidió explícitamente más tiempo (ej. 'llámenme la "
        "próxima semana' → 7) o si aplica un escenario de baja urgencia. Nunca "
        "calcules ni escribas una fecha vos mismo — el sistema convierte este "
        "número en la fecha real."
    ),
}

HORA_LIMITE_SCHEMA = {
    "type": "string",
    "pattern": r"^([01][0-9]|2[0-3]):[0-5][0-9]$",
    "description": (
        "Hora del día (formato 24h HH:MM, hora de Miami) en que el operador "
        "debe ejecutar la tarea. Orden de prioridad para decidirla:\n"
        "1. Si el cliente dio una hora EXPLÍCITA (ej. 'llámame a las 3pm', "
        "'mañana a las 10 de la mañana'), usa esa hora EXACTA convertida a "
        "24h — nunca la redondees ni la reinterpretes. 'llámame mañana a las "
        "03:00 pm' → hora_limite='15:00' y dias_para_vencer=1.\n"
        "2. Si el cliente dio un momento aproximado sin hora exacta ('en la "
        "mañana'/'temprano' → 09:00, 'al mediodía' → 12:00, 'en la tarde' → "
        "15:00, 'en la noche'/'antes de que cierren' → 18:00), usa esa "
        "traducción.\n"
        "3. Si el cliente no mencionó ningún momento, decide por urgencia: "
        "temperatura Caliente → lo antes posible en horario laboral; "
        "Tibio/Frio → 10:00 por defecto.\n"
        "Nunca uses horarios fuera de 08:00-18:00 (horario laboral), salvo "
        "que el cliente haya pedido explícitamente una hora fuera de ese "
        "rango — en ese caso respeta lo que pidió el cliente por sobre la "
        "regla de horario laboral."
    ),
}

TOOLS_BASE = [
    {
        "type": "function",
        "function": {
            "name": "generar_tarea_inmediata",
            "description": "El lead requiere atención humana inmediata. Genera una tarea NUEVA para el agente.",
            "parameters": {
                "type": "object",
                "properties": {
                    "razonamiento": RAZONAMIENTO_SCHEMA,
                    "temperatura": TEMPERATURA_SCHEMA,
                    "interes": {"type": "string", "enum": ["Casa", "Townhouse", "Inversión", "Rentar", "No determinado"]},
                    "calificacion_lead": {"type": "integer", "minimum": 1, "maximum": 10, "description": "1-10 basado en intención de compra."},
                    "titulo_tarea": {"type": "string", "description": "Título corto y accionable."},
                    "instruccion_para_agente": {"type": "string", "description": "Instrucción detallada. Si incluye un mensaje para enviar, redáctalo aquí aplicando las reglas de oro."},
                    "dias_para_vencer": DIAS_PARA_VENCER_SCHEMA,
                    "hora_limite": HORA_LIMITE_SCHEMA,
                    "nota_bitacora": NOTA_BITACORA_SCHEMA,
                },
                "required": [
                    "razonamiento", "temperatura", "interes",
                    "calificacion_lead", "titulo_tarea",
                    "instruccion_para_agente", "dias_para_vencer",
                    "hora_limite", "nota_bitacora",
                ],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "ia_puede_continuar",
            "description": "El mensaje del cliente es simple y el bot puede manejarlo sin intervención humana inmediata.",
            "parameters": {
                "type": "object",
                "properties": {
                    "razonamiento": RAZONAMIENTO_SCHEMA,
                    "temperatura": TEMPERATURA_SCHEMA,
                    "titulo_tarea": {
                        "type": "string",
                        "description": (
                            "Título corto de una sugerencia de seguimiento OPCIONAL para el agente humano, "
                            "detectada al leer la conversación (ej: '[Sugerencia] Intentar obtener correo', "
                            "'[Sugerencia] Confirmar cita', '[Sugerencia] Reagendar llamada'). "
                            "Usar string vacío '' si no hay ninguna sugerencia relevante en este momento."
                        ),
                    },
                    "nota_bitacora": NOTA_BITACORA_SCHEMA,
                },
                "required": ["razonamiento", "temperatura", "titulo_tarea", "nota_bitacora"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "marcar_sin_accion",
            "description": "El lead dijo STOP, no le llamen, o es spam. No se debe crear tarea ni contactar.",
            "parameters": {
                "type": "object",
                "properties": {
                    "motivo": {"type": "string", "description": "Motivo del descarte."},
                    "razonamiento": RAZONAMIENTO_SCHEMA,
                    "temperatura": TEMPERATURA_SCHEMA,
                    "nota_bitacora": NOTA_BITACORA_SCHEMA,
                },
                "required": ["motivo", "razonamiento", "temperatura", "nota_bitacora"],
            },
        },
    },
]


def _formatear_tareas_pendientes(tareas: list[dict]) -> str:
    """
    Convierte el array de tareas pendientes de GHL en un bloque de texto
    legible para el LLM, para que sepa qué ya está abierto y no lo duplique.
    """
    if not tareas:
        return "No hay tareas pendientes abiertas actualmente para este contacto."

    lineas = []
    for t in tareas:
        titulo = t.get("titulo", "(sin título)")
        descripcion_html = t.get("descripcion", "") or ""
        descripcion_limpia = re.sub(r"<[^>]+>", "", descripcion_html).strip()
        fecha_limite = t.get("fecha_limite", "sin fecha")
        task_id = t.get("id", "sin-id")
        lineas.append(f'- ID "{task_id}" | "{titulo}" (vence {fecha_limite}): {descripcion_limpia}')

    return "\n".join(lineas)


def _build_tools(tareas_pendientes_ghl: list[dict]) -> list[dict]:
    """
    Arma la lista de tools para esta corrida. La tool 'actualizar_tarea_existente'
    solo se ofrece si hay tareas abiertas, y su 'id_tarea_a_actualizar' se restringe
    por enum a los IDs reales que vinieron en el payload — así el LLM no puede
    inventar/alucinar un ID de tarea que no existe en GHL.
    """
    tools = list(TOOLS_BASE)

    ids_validos = [t.get("id") for t in tareas_pendientes_ghl if t.get("id")]
    if ids_validos:
        tools.append({
            "type": "function",
            "function": {
                "name": "actualizar_tarea_existente",
                "description": (
                    "Úsala SOLO si la nueva solicitud tiene el EXACTO MISMO PROPÓSITO "
                    "(misma acción, mismo asunto) que una tarea ya abierta en la lista. "
                    "Si el motivo, el contexto o la acción son distintos, NO la uses; "
                    "en su lugar, debes usar 'generar_tarea_inmediata'."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "razonamiento": RAZONAMIENTO_SCHEMA,
                        "temperatura": TEMPERATURA_SCHEMA,
                        "id_tarea_a_actualizar": {
                            "type": "string",
                            "enum": ids_validos,
                            "description": (
                                "ID exacto de la tarea a actualizar. Debe ser uno de los IDs "
                                "listados en 'Tareas pendientes ya abiertas'. NUNCA inventes uno."
                            ),
                        },
                        "nuevo_titulo": {
                            "type": "string",
                            "description": "Nuevo título de la tarea, o string vacío '' si el actual sigue siendo válido.",
                        },
                        "nueva_instruccion": {
                            "type": "string",
                            "description": "Nueva instrucción/descripción para el agente, o string vacío '' si la actual sigue siendo válida.",
                        },
                        "reagendar": {
                            "type": "boolean",
                            "description": (
                                "true si hay que cambiar cuándo se ejecuta la tarea (el cliente "
                                "pidió otro horario, no respondió y toca reintentar después, etc.). "
                                "false si la fecha/hora actual de la tarea sigue siendo válida y "
                                "solo cambia el título/instrucción, o ninguno de los dos."
                            ),
                        },
                        "nuevos_dias_para_vencer": DIAS_PARA_VENCER_SCHEMA,
                        "nueva_hora_limite": HORA_LIMITE_SCHEMA,
                        "nota_bitacora": NOTA_BITACORA_SCHEMA,
                    },
                    "required": [
                        "razonamiento", "temperatura", "id_tarea_a_actualizar",
                        "nuevo_titulo", "nueva_instruccion", "reagendar",
                        "nuevos_dias_para_vencer", "nueva_hora_limite",
                        "nota_bitacora",
                    ],
                },
            },
        })

    return tools


@lru_cache(maxsize=1)
def _get_static_prompt() -> str:
    return PROMPT_PATH.read_text(encoding="utf-8")


def analyze_pending_review(
    contact_name: str,
    historial_texto: str,
    tareas_pendientes_ghl: list[dict] | None = None,
    max_intentos: int = 2,
) -> dict:
    """
    Corre el razonamiento del LLM sobre el historial de un lead con actividad reciente.
    Recibe además las tareas ya abiertas en GHL para ese contacto, de modo que el LLM
    pueda optar por actualizar una existente (fecha/hora, título, instrucción) en vez
    de crear una nueva y duplicarla.

    Si el LLM devuelve JSON inválido en los argumentos de la tool, reintenta hasta
    `max_intentos` veces. Distingue dos causas posibles:
      1. Truncamiento por max_tokens (finish_reason == "length"): la respuesta se
         cortó a mitad de camino. Se reintenta con más tokens.
      2. JSON mal formado por otra razón (ej. comillas sin escapar): se reintenta
         con un recordatorio explícito del error.
    """
    tareas_pendientes_ghl = tareas_pendientes_ghl or []

    ahora_miami = datetime.now(MIAMI_TZ)
    fecha_hoy_str = _formatear_fecha_hoy(ahora_miami)

    prompt_base = _get_static_prompt().format(
        contact_name=contact_name,
        historial_texto=historial_texto,
        tareas_pendientes_existentes=_formatear_tareas_pendientes(tareas_pendientes_ghl),
        fecha_hoy=fecha_hoy_str,
    )

    tools = _build_tools(tareas_pendientes_ghl)

    client = get_openrouter_client()
    logger.info(f"🧠 Analizando pending review para '{contact_name}' | tools_disponibles={[t['function']['name'] for t in tools]}")

    model_name = os.environ.get("PENDING_REVIEWS_MODEL") or os.environ.get("COOL_LEADS_MODEL", "anthropic/claude-sonnet-5")
    temperature = float(os.environ.get("PENDING_REVIEWS_TEMPERATURE") or os.environ.get("COOL_LEADS_TEMPERATURE", "0.3"))
    max_tokens = int(os.environ.get("PENDING_REVIEWS_MAX_TOKENS") or os.environ.get("COOL_LEADS_MAX_TOKENS", "1800"))

    prompt_actual = prompt_base
    tokens_actuales = max_tokens
    ultimo_error: json.JSONDecodeError | None = None

    for intento in range(1, max_intentos + 1):
        response = client.chat.completions.create(
            model=model_name,
            temperature=temperature,
            max_tokens=tokens_actuales,
            messages=[{"role": "user", "content": prompt_actual}],
            tools=tools,
            tool_choice="required",
        )

        choice = response.choices[0]
        message = choice.message
        if not message.tool_calls:
            raise RuntimeError(
                f"El modelo {model_name} ignoró tool_choice='required' y devolvió texto plano. "
                f"Contenido: {message.content}"
            )

        tool_call = message.tool_calls[0]
        raw_arguments = tool_call.function.arguments
        finish_reason = getattr(choice, "finish_reason", None)
        fue_truncado = finish_reason == "length"

        try:
            args = json.loads(raw_arguments)
        except json.JSONDecodeError as e:
            ultimo_error = e
            logger.error(
                f"❌ Error decodificando JSON del LLM (intento {intento}/{max_intentos}) "
                f"| {contact_name} | finish_reason={finish_reason}: {e}"
            )
            logger.error(f"📄 Argumentos crudos recibidos del LLM:\n{raw_arguments}")

            if intento < max_intentos:
                if fue_truncado:
                    tokens_actuales = int(tokens_actuales * 1.5)
                    prompt_actual = (
                        f"{prompt_base}\n\n"
                        f"ADVERTENCIA: tu respuesta anterior se cortó antes de completar el JSON "
                        f"(te quedaste sin espacio). En 'nota_bitacora', si hay más de 8 hitos "
                        f"relevantes, resume los más antiguos en un solo renglón agregado y detalla "
                        f"solo los últimos 6-8 hitos más recientes. Sé más conciso en general y "
                        f"asegúrate de cerrar completamente el JSON con todos los campos requeridos."
                    )
                    logger.warning(
                        f"⚠️ Truncamiento detectado, reintentando con max_tokens={tokens_actuales} "
                        f"| {contact_name}"
                    )
                else:
                    prompt_actual = (
                        f"{prompt_base}\n\n"
                        f"ADVERTENCIA: tu respuesta anterior tenía JSON inválido y no pudo "
                        f"procesarse (error: {e}). La causa más común es una comilla doble (\") "
                        f"sin escapar dentro de un valor de texto. Genera de nuevo la respuesta "
                        f"completa, revisando que NINGÚN campo de texto contenga comillas dobles "
                        f"sin escapar."
                    )
                continue
            raise RuntimeError(
                f"El modelo devolvió JSON inválido en los argumentos de la herramienta "
                f"tras {max_intentos} intentos (finish_reason={finish_reason}). Error: {e}"
            )

        accion = tool_call.function.name
        logger.info(f"✅ Decisión: {accion} | {contact_name} | intento {intento}/{max_intentos}")

        return {"accion": accion, **args}

    raise RuntimeError(f"Fallo inesperado analizando a {contact_name}. Último error: {ultimo_error}")