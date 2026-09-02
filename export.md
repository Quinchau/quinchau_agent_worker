# Project Structure

```
app/
  __pycache__/
    __init__.cpython-312.pyc
    jobs.cpython-312.pyc
    main.cpython-312.pyc
    models.cpython-312.pyc
    redis_queue.cpython-312.pyc
    tasks.cpython-312.pyc
  contextos/
    realtor/
      __init__.py
      prompt_cool_leads.txt
      prompt_pending_reviews.txt
    prompt_clasificador_unificado.txt
    prompt_consulta_ubicacion_horario.txt
    prompt_envios_y_entregas.txt
    prompt_faq_redactor.txt
    prompt_faq_selector_general.txt
    prompt_faq_selector.txt
    prompt_intencion_compra_al_mayoreo.txt
    prompt_llm_generico_system.txt
    prompt_llm_generico_user.txt
    prompt_orden_sin_despacho.txt
    prompt_seleccion_catalogo_generica.txt
    prompt_seleccion_catalogo.txt
    prompt_seleccion_herramienta.txt
    prompt_seleccion_variante.txt
    prompt_sin_clasificar.txt
    prompt_solicitud_catalogo_modelo.txt
  intenciones/
    __init__.py
    busqueda_producto_generica.py
    busqueda_producto_modelo.py
    catalogo.py
    compra_al_mayoreo.py
    context.py
    generico.py
    informacion_general.py
    saludo.py
    sin_clasificar.py
    solicitud_catalogo_modelo.py
    variante_producto.py
  realtor/
    cool_leads/
      __init__.py
      reasoning.py
      tasks.py
    pending_reviews/
      ghl_helpers.py
      reasoning.py
  __init__.py
  agent_state.py
  agent.py
  catalog_cache.py
  classifier_block_builder.py
  classifier.py
  database.py
  entity_resolver.py
  ghl.py
  jobs.py
  llm_client.py
  main.py
  models.py
  product_search_cache.py
  prompts.py
  redis_queue.py
  tasks.py
  test_bloque.py
  test_clasificador.py
  test_validar.py
  ver_cola.py
  worker_realtor.py
  worker.py
codigo_muerto/
  aclaracion_gate_for_eraser.py
  envios_y_entregas.py
  generar_tools.py
  intent_classifier.py
  inyection_prompt_from_tool_deprecated.py
  orden_sin_despacho.py
  prompt_aclaracion_gate.txt
  prompt_entidades_faltantes.txt
  prompt_intencion_retiro_y_pago_personal.txt
  prompt_intent_classifier.txt
  prompt_selector_producto.txt
  templates.py
  ubicacion_horario.py
plans/
  plan-mysql-connection-pooling.md
skills/
.env
.gitignore
Dockerfile-agent
export.md
index_products.py
indexador_unificado.py
Quinchau_Agent_Indexacion.docx
README.md
reindex_multi_term.py
requirements.txt
test_search.py
```



# Selected Files Content

## app/contextos/realtor/__init__.py

```py

```

## app/contextos/realtor/prompt_cool_leads.txt

```txt
Sos un asistente que analiza el historial de conversación de un lead inmobiliario que lleva
{days_inactive} días sin responder, para recomendar la próxima acción al vendedor humano.

Tu rol es sugerir, no decidir. Siempre dejá un motivo claro y accionable.

Reglas de análisis (nivel de interés):
- Interés alto: el lead mostró intención concreta (preguntó precio, pidió visita, dio disponibilidad)
  antes de desaparecer.
  → crear_tarea con tipo_tarea="reactivacion", mensaje personalizado.
- Interés medio: hubo intercambio genuino pero sin señales fuertes de intención de compra/alquiler.
  → crear_tarea con tipo_tarea="reactivacion", seguimiento suave.
- Interés bajo: respuestas cortas, dudas sin resolver, o desinterés implícito.
  → crear_tarea con tipo_tarea="marcar_lost", sugiriendo al vendedor pasar el lead a Lost.
- Interés nulo: spam, número equivocado, el lead ya cerró por otro medio, o pidió
  explícitamente no ser contactado.
  → descartar. No se crea tarea; el vendedor no necesita hacer nada con este lead.

Reglas de umbral por inactividad (tienen prioridad sobre el nivel de interés,
salvo que el nivel de interés sea nulo, en cuyo caso siempre corresponde descartar):

1. Corte duro (más de 365 días): si el lead lleva más de un año sin responder ni
   contactarse Y no calificó como interés nulo, corresponde crear_tarea con
   tipo_tarea="marcar_lost", SIN EXCEPCIÓN — incluso si en su momento mostró interés
   alto o concreto. Esta regla anula la posibilidad de sugerir reactivación.

2. Corte estándar (más de 180 días): si el lead lleva más de 180 días inactivo,
   no calificó como interés nulo, y NO mostró interés concreto (interés alto),
   corresponde crear_tarea con tipo_tarea="marcar_lost".

Orden de evaluación: primero verificá si el nivel de interés es nulo → descartar,
sin importar los días. Si no es nulo, aplicá la regla de corte duro (365 días).
Si no aplica, evaluá la regla de corte estándar (180 días). Si ninguna aplica,
basá la recomendación en el nivel de interés (alto/medio/bajo).

Instrucciones adicionales:
- Basate únicamente en la información provista en el historial. No inventes datos,
  fechas ni afirmaciones que no estén explícitas en los mensajes.
- El historial puede incluir mensajes de más de una conversación; están ordenados
  cronológicamente, pero pueden provenir de canales o campañas distintas.
- Si corresponde crear tarea (reactivación o sugerencia de Lost), llamá a crear_tarea
  con un título y cuerpo concretos, indicando el tipo_tarea correspondiente.
- Si corresponde descartar, llamá a descartar con el motivo claro, dejando constancia
  de por qué se considera interés nulo.

Siempre completá "razonamiento" primero, en 2-4 oraciones, explicando en qué te basaste.

Contacto: {contact_name}
Total de mensajes: {total_messages}
Días inactivo: {days_inactive}

Historial:
{historial_texto}
```

## app/contextos/realtor/prompt_pending_reviews.txt

```txt

```

## app/realtor/cool_leads/__init__.py

```py

```

## app/realtor/cool_leads/reasoning.py

```py
import json
import logging
import os
from functools import lru_cache
from pathlib import Path

from app.llm_client import get_openrouter_client

logger = logging.getLogger(__name__)

PROMPT_PATH = Path(__file__).resolve().parents[2] / "contextos" / "realtor" / "prompt_cool_leads.txt"

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "crear_tarea",
            "description": (
                "El vendedor debe tomar una acción sobre este lead: reactivarlo o marcarlo "
                "como Lost. SIEMPRE se crea una tarea visible para el vendedor."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "razonamiento": {"type": "string", "description": "Análisis paso a paso antes de decidir."},
                    "nivel_interes": {"type": "string", "enum": ["alto", "medio", "bajo"]},
                    "tipo_tarea": {
                        "type": "string",
                        "enum": ["reactivacion", "marcar_lost"],
                        "description": (
                            "'reactivacion' si el lead vale la pena retomar. "
                            "'marcar_lost' si corresponde sugerir al vendedor pasar el lead a Lost "
                            "(por interés bajo o por regla de umbral de inactividad)."
                        ),
                    },
                    "task_title": {"type": "string"},
                    "task_body": {"type": "string"},
                    "prioridad": {"type": "string", "enum": ["alta", "media", "baja"]},
                    "dias_para_seguimiento": {"type": "integer"},
                },
                "required": [
                    "razonamiento", "nivel_interes", "tipo_tarea",
                    "task_title", "task_body", "prioridad", "dias_para_seguimiento",
                ],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "descartar",
            "description": (
                "El vendedor NO debe tomar ninguna acción. No se crea tarea. "
                "Usar SOLO para casos de interés nulo real: spam, número equivocado, "
                "el lead ya cerró por otro medio, o pidió explícitamente no ser contactado."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "razonamiento": {"type": "string", "description": "Análisis paso a paso antes de decidir."},
                    "nivel_interes": {"type": "string", "enum": ["nulo"]},
                    "motivo": {"type": "string", "description": "Motivo breve del descarte, para dejar registro."},
                },
                "required": ["razonamiento", "nivel_interes", "motivo"],
            },
        },
    },
]


@lru_cache(maxsize=1)
def _get_static_prompt() -> str:
    return PROMPT_PATH.read_text(encoding="utf-8")


def analyze_lead(contact_name: str, days_inactive: int, total_messages: int, historial_texto: str) -> dict:
    """
    Corre el razonamiento del LLM sobre el historial de un lead.
    NO atrapa excepciones (Modo B, §7.7): si el LLM falla, el job de RQ debe fallar visiblemente.
    """
    prompt = _get_static_prompt().format(
        contact_name=contact_name,
        days_inactive=days_inactive,
        total_messages=total_messages,
        historial_texto=historial_texto,
    )

    client = get_openrouter_client()
    logger.info(f"🧠 Analizando lead '{contact_name}' ({days_inactive} días inactivo)")

    response = client.chat.completions.create(
        model=os.environ["COOL_LEADS_MODEL"],
        temperature=float(os.environ.get("COOL_LEADS_TEMPERATURE", "0.3")),
        max_tokens=int(os.environ.get("COOL_LEADS_MAX_TOKENS", "700")),
        messages=[{"role": "user", "content": prompt}],
        tools=TOOLS,
        tool_choice="required",
    )

    message = response.choices[0].message
    if not message.tool_calls:
        raise RuntimeError(
            f"El modelo {os.environ['COOL_LEADS_MODEL']} ignoró tool_choice='required' "
            f"y devolvió texto plano en lugar de una función. Contenido: {message.content}"
        )

    tool_call = message.tool_calls[0]
    args = json.loads(tool_call.function.arguments)

    accion = "crear_tarea" if tool_call.function.name == "crear_tarea" else "descartar"

    logger.info(
        f"✅ Decisión: {accion} | tipo_tarea={args.get('tipo_tarea', 'n/a')} | "
        f"interés={args.get('nivel_interes')} | {contact_name}"
    )

    return {"accion": accion, **args}
```

## app/realtor/cool_leads/tasks.py

```py
import logging
import os
from datetime import date, timedelta

import httpx

from app.realtor.cool_leads.reasoning import analyze_lead

logger = logging.getLogger(__name__)

SUGGESTION_PREFIX = "[🤖 Sugerencia]"
ACCIONES_VALIDAS = {"crear_tarea", "descartar"}
DUE_DATE_HORA = "09:00 AM"  # hora fija para el Due de las tareas sugeridas


def _ensure_suggestion_prefix(title: str) -> str:
    """
    Garantiza que el título de la tarea comience con el prefijo de sugerencia AI.
    Esto permite:
    1. Que el vendedor distinga visualmente tareas generadas por el sistema.
    2. Rollback masivo en GHL filtrando por este prefijo.
    3. Medición de efectividad (tareas AI completadas vs ignoradas).
    """
    if not title:
        return SUGGESTION_PREFIX
    if title.startswith(SUGGESTION_PREFIX):
        return title
    return f"{SUGGESTION_PREFIX} {title}"


def _build_due_date(dias_seguimiento: int) -> str:
    """
    Calcula el Due Date ya formateado como lo exige GHL (MM-DD-YYYY HH:MM AM/PM).

    IMPORTANTE: el workflow de GHL mapea el campo DUE DATE directamente desde el
    payload del webhook, sin ningún cálculo intermedio. Antes se le pasaba
    `dias_seguimiento` (un entero crudo) y GHL, al no poder parsearlo como fecha,
    caía a epoch 0 → "Dec 31 1969, 7:00 PM (EDT)". Por eso acá resolvemos la
    fecha final nosotros mismos, en el formato exacto que GHL espera.

    - reactivacion: hoy + dias_seguimiento (mínimo 1 día).
    - marcar_lost: no hay "seguimiento" real (el LLM suele mandar 0), pero la
      tarea igual necesita un Due válido y cercano → hoy + 1 día.
    """
    dias_para_due = dias_seguimiento if dias_seguimiento > 0 else 1
    due_date_dt = date.today() + timedelta(days=dias_para_due)
    return f"{due_date_dt.strftime('%m-%d-%Y')} {DUE_DATE_HORA}"


def process_cool_lead(payload: dict) -> None:
    """
    Entrypoint del job RQ.
    Modo B (§7.7): no atrapamos excepciones acá — si algo falla, el job debe quedar visible
    en FailedJobRegistry para reintento o revisión manual.

    - accion="crear_tarea": SIEMPRE se crea una tarea visible para el vendedor (ya sea
      para reactivar el lead o para sugerirle marcarlo como Lost, según tipo_tarea).
    - accion="descartar": NO se crea tarea. El vendedor no necesita hacer nada; solo
      se deja registro del motivo para auditoría.
    """
    webhook_url = os.environ.get("COOL_LEADS_WEBHOOK_URL")
    if not webhook_url:
        raise RuntimeError("COOL_LEADS_WEBHOOK_URL no está seteada en el entorno del Worker.")

    lead_id = payload.get("cool_lead_id") or payload.get("task_id", "unknown_id")

    try:
        contact_id = payload["contact_id"]
        contact_name = payload["contact_name"]
    except KeyError as e:
        logger.error(f"❌ Payload incompleto | lead_id={lead_id} | falta clave={e} | payload={payload}")
        raise

    logger.info(f"📩 Job recibido | contact_id={contact_id} | lead_id={lead_id}")

    result = analyze_lead(
        contact_name=contact_name,
        days_inactive=payload["days_inactive"],
        total_messages=payload["total_messages"],
        historial_texto=payload["historial_texto"],
    )

    logger.info(f"🧠 Razonamiento LLM | {contact_name}: {result.get('razonamiento', 'N/A')}")

    accion = result.get("accion")
    if accion not in ACCIONES_VALIDAS:
        logger.warning(f"⚠️ Acción inesperada del LLM: {accion!r} | lead_id={lead_id} | {contact_name}")
        accion = "descartar"

    dias_seguimiento_raw = result.get("dias_para_seguimiento", 0)
    try:
        dias_seguimiento = max(0, min(30, int(dias_seguimiento_raw)))
    except (TypeError, ValueError):
        logger.warning(f"⚠️ dias_para_seguimiento no numérico: {dias_seguimiento_raw!r} | {contact_name}")
        dias_seguimiento = 0

    tipo_tarea = result.get("tipo_tarea", "")

    if accion == "crear_tarea":
        # Cubre ambos casos: reactivación y sugerencia de marcar Lost.
        # El contenido siempre viene del LLM — no se hardcodea texto acá.
        final_title = _ensure_suggestion_prefix(result.get("task_title", ""))
        final_body = result.get("task_body") or "Sin cuerpo especificado por el LLM."
        if not result.get("task_title") or not result.get("task_body"):
            logger.warning(
                f"⚠️ task_title/task_body vacío para accion=crear_tarea | "
                f"tipo_tarea={tipo_tarea} | {contact_name}"
            )
        due_date = _build_due_date(dias_seguimiento)
    else:
        # descartar: no se crea tarea. Campos de tarea vacíos a propósito;
        # el workflow de GHL no debe consumirlos en esta rama.
        final_title = ""
        final_body = ""
        due_date = ""

    webhook_payload = {
        "cool_lead_id": lead_id,
        "contact_id": contact_id,
        "contact_name": contact_name,
        "accion": accion,  # "crear_tarea" o "descartar"
        "tipo_tarea": tipo_tarea,  # "reactivacion" | "marcar_lost" | "" (si descartar)
        "nivel_interes": result.get("nivel_interes", "desconocido"),
        "task_title": final_title,
        "task_body": final_body,
        "motivo_descarte": result.get("motivo", ""),
        "dias_seguimiento": dias_seguimiento,  # se conserva por compatibilidad/debug
        "due_date": due_date,  # <-- nuevo: ya formateado para GHL (MM-DD-YYYY HH:MM AM/PM)
        "custom_field_value": date.today().strftime("%m-%d-%Y"),
    }

    logger.info(f"📦 Webhook payload: {webhook_payload}")
    logger.info(f"🚀 Disparando webhook | accion={accion} | tipo_tarea={tipo_tarea} | {contact_name}")

    response = httpx.post(webhook_url, json=webhook_payload, timeout=15)
    response.raise_for_status()

    logger.info(f"✅ Webhook OK | {contact_name} | trigger_id={response.json().get('id')}")
```

## app/realtor/pending_reviews/ghl_helpers.py

```py

```

## app/realtor/pending_reviews/reasoning.py

```py

```

## app/worker_realtor.py

```py
"""
RQ Worker — Dedicado exclusivamente al procesamiento de trabajos del dominio Realtor (ej. Cool Leads).

Arrancar localmente con:
    python app/worker_realtor.py

O en Docker con el comando definido en docker-compose (servicio quinchau-cool-leads-worker).
Escucha únicamente las colas de Realtor.
"""

import os
import sys
import logging

# ============================================
# CONFIGURACIÓN DE LOGGING
# ============================================
DEBUG = os.getenv("DEBUG", "false").lower() == "true"

logging.basicConfig(
    level=logging.DEBUG if DEBUG else logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    datefmt="%H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout)
    ]
)

# ============================================
# AGREGAR RUTA DEL PROYECTO AL PYTHONPATH
# ============================================
# Esto permite que los imports "from app.xxx" funcionen correctamente
ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

from rq import Worker
from app.redis_queue import get_redis, QUEUE_REALTOR_COOL_LEADS

if __name__ == "__main__":
    redis_conn = get_redis()
    
    # Definición explícita de las colas que procesa este worker dedicado
    queues = [QUEUE_REALTOR_COOL_LEADS]

    logging.info("=" * 60)
    logging.info("🚀 Worker Realtor iniciando")
    logging.info(f"📂 ROOT_DIR: {ROOT_DIR}")
    logging.info(f"📬 Escuchando cola(s): {queues}")
    logging.info(f"🔧 DEBUG: {DEBUG}")
    logging.info("=" * 60)
    
    # FIX: Eliminamos with_scheduler=True porque el Gate se encarga del encolado.
    # El worker solo debe consumir jobs inmediatos de la cola.
    worker = Worker(queues, connection=redis_conn)
    
    try:
        worker.work()
    except KeyboardInterrupt:
        logging.info("🛑 Worker detenido manualmente (KeyboardInterrupt)")
    except Exception as e:
        logging.error(f"💥 Worker falló inesperadamente: {e}", exc_info=True)
        raise
```

