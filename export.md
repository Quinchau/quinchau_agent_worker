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
      tasks.py
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
  prompt_clasificador_unificado.txt
  prompts.py
  redis_queue.py
  tasks.py
  test_bloque.py
  test_clasificador.py
  test_validar.py
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

## app/realtor/pending_reviews/ghl_helpers.py

```py
import logging
import os
from datetime import datetime, timezone

import httpx

logger = logging.getLogger(__name__)


def actualizar_ultima_revision_ia(contact_id: str) -> None:
    """
    Actualiza el custom field 'ultima_revision_ia' en GHL con la fecha/hora actual.
    """
    # Usamos las variables genéricas o las específicas de Miami si existen
    api_key = os.environ.get("GHL_PRIVATE_TOKEN_FRANCHESCA_QUINTERO_TEAM_MIAMI") or os.environ.get("GHL_PRIVATE_TOKEN")
    cf_id = os.environ.get("GHL_CF_ULTIMA_REVISION_IA_ID")
    
    if not api_key or not cf_id:
        logger.warning("⚠️ Faltan credenciales de GHL (GHL_PRIVATE_TOKEN o GHL_CF_ULTIMA_REVISION_IA_ID) para actualizar custom field")
        return
    
    url = f"https://services.leadconnectorhq.com/contacts/{contact_id}"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Version": "2021-07-28",
        "Content-Type": "application/json",
    }
    
    payload = {
        "customFields": [
            {
                "id": cf_id,
                "value": datetime.now(timezone.utc).isoformat()
            }
        ]
    }
    
    try:
        response = httpx.put(url, headers=headers, json=payload, timeout=10.0)
        response.raise_for_status()
        logger.info(f"✅ Custom field 'ultima_revision_ia' actualizado para {contact_id}")
    except Exception as e:
        logger.error(f"❌ Error actualizando custom field para {contact_id}: {e}")
```

## app/realtor/pending_reviews/reasoning.py

```py
import json
import logging
import os
from functools import lru_cache
from pathlib import Path

from app.llm_client import get_openrouter_client

logger = logging.getLogger(__name__)

PROMPT_PATH = Path(__file__).resolve().parents[2] / "contextos" / "realtor" / "prompt_pending_reviews.txt"

TOOLS_PENDING_REVIEWS = [
    {
        "type": "function",
        "function": {
            "name": "generar_tarea_inmediata",
            "description": "El lead requiere atención humana inmediata. Genera una tarea clara para el agente.",
            "parameters": {
                "type": "object",
                "properties": {
                    "razonamiento": {"type": "string","description": "Análisis MUY CONCISO. Máximo 3-4 oraciones (aprox. 10-15 líneas de texto o 300 tokens). Ve directo al grano: qué dijo el cliente y por qué se toma esta decisión."},
                    "temperatura": {"type": "string", "enum": ["Caliente", "Tibio", "Frío", "No interesado"]},
                    "interes": {"type": "string", "enum": ["Casa", "Townhouse", "Inversión", "Rentar", "No determinado"]},
                    "calificacion_lead": {"type": "integer", "minimum": 1, "maximum": 10, "description": "1-10 basado en intención de compra."},
                    "titulo_tarea": {"type": "string", "description": "Título corto y accionable."},
                    "instruccion_para_agente": {"type": "string", "description": "Instrucción detallada. Si incluye un mensaje para enviar, redáctalo aquí aplicando las reglas de oro."}
                },
                "required": ["razonamiento", "temperatura", "interes", "calificacion_lead", "titulo_tarea", "instruccion_para_agente"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "ia_puede_continuar",
            "description": "El mensaje del cliente es simple y el bot puede manejarlo sin intervención humana inmediata.",
            "parameters": {
                "type": "object",
                "properties": {
                    "razonamiento": {"type": "string","description": "Análisis MUY CONCISO. Máximo 3-4 oraciones (aprox. 10-15 líneas de texto o 300 tokens). Ve directo al grano: qué dijo el cliente y por qué se toma esta decisión."},
                    "titulo_tarea": {
                        "type": "string",
                        "description": (
                            "Título corto de una sugerencia de seguimiento OPCIONAL para el agente humano, "
                            "detectada al leer la conversación (ej: '[Sugerencia] Intentar obtener correo', "
                            "'[Sugerencia] Confirmar cita', '[Sugerencia] Reagendar llamada'). "
                            "Usar string vacío '' si no hay ninguna sugerencia relevante en este momento."
                        )
                    }
                },
                "required": ["razonamiento", "titulo_tarea"]
            }
        }
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
                    "razonamiento": {"type": "string","description": "Análisis MUY CONCISO. Máximo 3-4 oraciones (aprox. 10-15 líneas de texto o 300 tokens). Ve directo al grano: qué dijo el cliente y por qué se toma esta decisión."},
                },
                "required": ["motivo", "razonamiento"]
            }
        }
    }
]


@lru_cache(maxsize=1)
def _get_static_prompt() -> str:
    return PROMPT_PATH.read_text(encoding="utf-8")


def analyze_pending_review(contact_name: str, historial_texto: str) -> dict:
    """
    Corre el razonamiento del LLM sobre el historial de un lead con actividad reciente.
    Incluye diagnóstico de JSON para depurar errores de formato del LLM.
    """
    prompt = _get_static_prompt().format(
        contact_name=contact_name,
        historial_texto=historial_texto,
    )

    client = get_openrouter_client()
    logger.info(f"🧠 Analizando pending review para '{contact_name}'")

    model_name = os.environ.get("PENDING_REVIEWS_MODEL") or os.environ.get("COOL_LEADS_MODEL", "anthropic/claude-sonnet-5")
    temperature = float(os.environ.get("PENDING_REVIEWS_TEMPERATURE") or os.environ.get("COOL_LEADS_TEMPERATURE", "0.3"))
    max_tokens = int(os.environ.get("PENDING_REVIEWS_MAX_TOKENS") or os.environ.get("COOL_LEADS_MAX_TOKENS", "800"))

    response = client.chat.completions.create(
        model=model_name,
        temperature=temperature,
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": prompt}],
        tools=TOOLS_PENDING_REVIEWS,
        tool_choice="required",
    )

    message = response.choices[0].message
    if not message.tool_calls:
        raise RuntimeError(
            f"El modelo {model_name} ignoró tool_choice='required' y devolvió texto plano. "
            f"Contenido: {message.content}"
        )

    tool_call = message.tool_calls[0]
    raw_arguments = tool_call.function.arguments
    
    # 👉 DIAGNÓSTICO DE JSON: Si el LLM genera JSON inválido, lo registramos para ver el error exacto.
    try:
        args = json.loads(raw_arguments)
    except json.JSONDecodeError as e:
        logger.error(f"❌ Error decodificando JSON del LLM: {e}")
        logger.error(f"📄 Argumentos crudos recibidos del LLM:\n{raw_arguments}")
        raise RuntimeError(f"El modelo devolvió JSON inválido en los argumentos de la herramienta. Error: {e}")

    accion = tool_call.function.name
    logger.info(f"✅ Decisión: {accion} | {contact_name}")

    return {"accion": accion, **args}
```

## app/realtor/pending_reviews/tasks.py

```py
import logging
import os
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx

from app.realtor.pending_reviews.reasoning import analyze_pending_review

logger = logging.getLogger(__name__)

ACCIONES_VALIDAS = {"generar_tarea_inmediata", "ia_puede_continuar", "marcar_sin_accion"}

# 👉 Zona horaria de Miami (maneja automáticamente EST/EDT)
MIAMI_TZ = ZoneInfo("America/New_York")


def process_pending_review(payload: dict) -> None:
    """
    Entrypoint del job RQ para Pending Reviews.
    Procesa leads con actividad reciente y oportunidad abierta.
    Delega la actualización del custom field al Workflow de GHL a través del webhook.
    """
    webhook_url = os.environ.get("PENDING_REVIEWS_WEBHOOK_URL")
    if not webhook_url:
        raise RuntimeError("PENDING_REVIEWS_WEBHOOK_URL no está seteada en el entorno del Worker.")

    task_id = payload.get("task_id", "unknown_id")

    try:
        contact_id = payload["contact_id"]
        contact_name = payload["contact_name"]
        historial_texto = payload["historial_texto"]
    except KeyError as e:
        logger.error(f"❌ Payload incompleto | task_id={task_id} | falta clave={e}")
        raise

    logger.info(f"📩 Job recibido | contact_id={contact_id} | task_id={task_id}")

    # 1. Analizar con el LLM
    result = analyze_pending_review(
        contact_name=contact_name,
        historial_texto=historial_texto,
    )

    logger.info(f"🧠 Razonamiento LLM | {contact_name}: {result.get('razonamiento', 'N/A')}")

    accion = result.get("accion")
    if accion not in ACCIONES_VALIDAS:
        logger.warning(f"⚠️ Acción inesperada del LLM: {accion!r} | task_id={task_id} | {contact_name}")
        accion = "marcar_sin_accion"

    # 👉 Fecha y hora exacta de la revisión de la IA en HORA DE MIAMI
    # Formato MM-DD-YYYY HH:MM:SS (compatible con la mayoría de parsers de GHL)
    fecha_hora_revision = datetime.now(MIAMI_TZ).strftime("%m-%d-%Y %H:%M:%S")

    # 2. Construir payload para el webhook
    # NOTA: "task_title" siempre viaja en el payload (poblado o "") en las tres ramas,
    # para que el workflow de GHL nunca reciba la key ausente y falle al mapear.
    if accion == "generar_tarea_inmediata":
        webhook_payload = {
            "task_id": task_id,
            "contact_id": contact_id,
            "contact_name": contact_name,
            "accion": "crear_tarea",
            "temperatura": result.get("temperatura", "No determinado"),
            "interes": result.get("interes", "No determinado"),
            "calificacion_lead": result.get("calificacion_lead", 5),
            "task_title": result.get("titulo_tarea", ""),
            "task_body": result.get("instruccion_para_agente", ""),
            "razonamiento": result.get("razonamiento", ""),
            "custom_field_value": fecha_hora_revision,
        }
    elif accion == "ia_puede_continuar":
        temperatura = result.get("temperatura", "No determinado")
        # Enriquecemos el razonamiento con la temperatura para que el agente tenga contexto rápido en GHL
        razonamiento_con_temperatura = f"Temperatura: {temperatura}\n\n{result.get('razonamiento', '')}"

        webhook_payload = {
            "task_id": task_id,
            "contact_id": contact_id,
            "contact_name": contact_name,
            "accion": "ia_continua",
            "task_title": result.get("titulo_tarea", ""),  # Sugerencia libre del LLM, o "" si no aplica
            "razonamiento": razonamiento_con_temperatura,
            "custom_field_value": fecha_hora_revision,
        }
    else:  # marcar_sin_accion
        webhook_payload = {
            "task_id": task_id,
            "contact_id": contact_id,
            "contact_name": contact_name,
            "accion": "descartar",
            "task_title": "",  # Nunca aplica una sugerencia aquí
            "motivo": result.get("motivo", ""),
            "razonamiento": result.get("razonamiento", ""),
            "custom_field_value": fecha_hora_revision,
        }

    # 3. Enviar webhook
    logger.info(f"📦 Webhook payload: {webhook_payload}")
    logger.info(f"🚀 Disparando webhook | accion={accion} | {contact_name}")

    response = httpx.post(webhook_url, json=webhook_payload, timeout=15)
    response.raise_for_status()

    logger.info(f"✅ Webhook OK | {contact_name} | trigger_id={response.json().get('id')}")
```

