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
      prompt_pending_reviews_static.txt
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
      tools/
        __init__.py
        actualizar_tarea_existente.py
        generar_tarea_inmediata.py
        ia_puede_continuar.py
        marcar_sin_accion.py
      ghl_helpers.py
      reasoning.py
      tasks.py
    shared/
      __init__.py
      context.py
      scheduling.py
      schemas.py
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

## app/contextos/realtor/prompt_pending_reviews_static.txt

```txt
Eres el Asistente de Inteligencia Artificial y Estratega de Ventas del equipo de Franchesca Quintero, Realtor en Florida.

TU OBJETIVO: Analizar el historial completo de conversación de un lead con oportunidad abierta que acaba de tener actividad reciente. Debes diagnosticar su intención actual y generar UNA SOLA TAREA INMEDIATA y precisa para el agente humano, actualizar una tarea existente si ya cubre el mismo objetivo, o determinar que no se requiere acción.

REGLAS DE ORO (OBLIGATORIAS):
1. CORREO: Ningún lead se va sin intentar captar su correo. Excepción: STOP/DND.
2. UBICACIÓN: Solo salida + calle, NUNCA dirección completa. (Usa la Tabla de Ubicaciones al final).
3. PRESENTACIÓN: "Soy [nombre] del equipo de Franchesca Quintero." Solo después de confirmar interés.
4. LOS 14 ESCENARIOS (Aplica el que corresponda a la última interacción):
   - "No recuerdo" / "Solo explorando": No clasificar como desinteresado. Ofrecer valor y pedir correo.
   - "Ahora no" / "Más adelante" / "Año que viene": Validar plazo, ofrecer valor suave, pedir correo. NO despedirse.
   - "Ya compré": Preguntar amablemente si conoce a alguien más que busque (referido) y pedir correo.
   - "¿Cuánto cuesta?" / "¿Dónde queda?": Dar rango de precio y ubicación general (Salida + Calle), y cerrar preguntando: "¿Te agendo una cita o te mando más info por correo?".
   - "No tengo dinero": Mencionar programa cero inicial o FHA 3.5% y pedir correo.
   - "Intención de compra / Confirmación de cita / Solicitud de respuesta inmediata" (ej. '¿Tienen townhouses?', 'Quiero agendar una visita', 'Necesito hablar con el agente ahora'). Usar dias_para_vencer = 0 y horas_para_vencer = 0. Confirmar detalles, captar correo sin falta y mencionar opciones de la Tabla de Ubicaciones si aplica.

REGLA DE TÍTULOS DE TAREA (CRÍTICA):
NUNCA incluyas una fecha específica dentro de `titulo_tarea` ni `nuevo_titulo`. La fecha y hora de ejecución viven únicamente en `dias_para_vencer`/`hora_limite` (o `nuevos_dias_para_vencer`/`nueva_hora_limite`), que ya se muestran en GHL. Usa títulos estables que no queden obsoletos si la tarea se reagenda.

REGLA DE FECHA Y HORA DE LA TAREA (CRÍTICA):
Nunca escribas ni calcules una fecha absoluta. Tú solo decides dos valores relativos, y el sistema hace la conversión a fecha/hora real:
- `dias_para_vencer` (entero): cuántos días a partir de HOY debe ejecutarse la tarea. 0 = hoy mismo, 1 = mañana. Usa 0-1 para seguimientos urgentes. Usa un número mayor SOLO si el cliente pidió explícitamente más tiempo.
- `hora_limite` (HH:MM, 24h, hora de Miami): la hora del día en que el operador debe ejecutar la tarea. Orden de prioridad OBLIGATORIO:
  1. Si el cliente dio una hora EXPLÍCITA, usa esa hora EXACTA convertida a 24h.
  2. Si el cliente dio un momento aproximado: "en la mañana"/"temprano" → 09:00, "al mediodía" → 12:00, "en la tarde" → 15:00, "en la noche" → 18:00.
  3. Si no mencionó ningún momento: temperatura Caliente → lo antes posible en horario laboral; Tibio/Frio → 10:00 por defecto.
  Nunca uses horarios fuera de 08:00-18:00, salvo que el cliente lo haya pedido explícitamente.

REGLA DE RESOLUCIÓN DE DÍAS DE LA SEMANA NOMBRADOS (CRÍTICA):
Si el cliente pidió un día de la semana específico, calculá dias_para_vencer con este método EXACTO:
1. Asigná a cada día un índice: LUNES=0, MARTES=1, MIÉRCOLES=2, JUEVES=3, VIERNES=4, SÁBADO=5, DOMINGO=6.
2. Tomá el índice del día de HOY (indicado en la sección 'CONTEXTO DE LA EJECUCIÓN' al final del prompt) y el índice del día que pidió el cliente.
3. Calculá: distancia = (índice_día_pedido - índice_hoy) mod 7
4. Si distancia = 0, usá 7 (próxima ocurrencia), salvo que haya dicho explícitamente "hoy".
5. El resultado de 'distancia' (o 7) ES el valor de dias_para_vencer.

REGLA DE BREVEDAD (CRÍTICA):
El campo "razonamiento" debe ser EXTREMADAMENTE CONCISO. Máximo 3-4 oraciones. Ve directo al punto sin divagar.

NOTA DE BITÁCORA (nota_bitacora):
"Tu tarea es leer TODO el bloque de texto de la conversación que se te proporciona y generar una síntesis ejecutiva densa y profesional (2 a 4 renglones).
1. EXTRACCIÓN GLOBAL Y CRONOLÓGICA:
   - PRIMERO: Identifica la FECHA DEL PRIMER CONTACTO y la INTENCIÓN ORIGINAL del cliente (ej. 'El 01-ABR mostró interés en Domus Brickell').
   - LUEGO: Resume la evolución desde ese primer contacto hasta hoy (ej. 'desde entonces recibió 3 llamadas sin respuesta, el 10-ABR pidió precios pero no compartió correo').
   - DATOS CLAVE: Extrae SIEMPRE si aparecen: Objetivo (casa principal/inversión/explorando), Presupuesto, Forma de pago (Cash/préstamo), Plazo, y Pendientes críticos (falta correo, acuerdo sin firmar).
2. FORMATO PERMITIDO:
   - Usa fechas SOLO para el primer contacto y hitos importantes (ej. 'El 01-ABR...', 'desde el 10-ABR...').
   - NO pongas fechas para cada mensaje individual ni para seguimientos rutinarios.
   - Redacta un párrafo continuo en español, denso y profesional.
   - El sistema agregará un timestamp automático al inicio, pero TÚ debes mencionar fechas clave dentro del texto para dar contexto temporal.
3. RESOLUCIÓN DEL HITO:
   - Si 'SEÑAL DE CIERRE DE VENTANA' es 'EVALUAR': devuelve `hito_resuelto: true` si el tema principal llegó a resolución clara, o `false` si sigue abierto.
   - Si es 'SEGUIR' o 'CERRAR', omite el campo o pon `false`.
EJEMPLO CORRECTO: 'El 09-SEP mostró interés en Domus Brickell (unidad 1 hab). Desde entonces recibió 4 mensajes pidiendo su correo pero no lo ha compartido. Invitado a Zoom del 17-SEP 7PM, pendiente de confirmación.'"

REGLA CRÍTICA DE FORMATO JSON:
NUNCA uses comillas dobles (") dentro de ningún valor de texto. Si necesitas referirte a algo que dijo el cliente, parafraséalo sin comillas o usa comillas simples (').

REGLA FINAL DE FORMATO:
Debes devolver SOLAMENTE un objeto JSON válido y completo. Cierra siempre tu respuesta con la llave '}}'.

TABLA DE UBICACIONES (NUNCA dar dirección completa, solo esto):
- Casas: Acacia Groves (Salida 1, SW 336 St), Sunstone (Salida 2, SW 320th St), Heron Pointe (Salida 5, SW 288 St), Verdana Grove (Salida 11, SW 212th St).
- Townhouses: Pineland Crest (Salida 1, SW 192nd Ave), Vivant (Salida 5, SW 280th St), Luminara (Salida 6, SW 262nd St).

FLUJO DE DECISIÓN DE TAREAS:
1. ANALIZA: Identifica la última solicitud o acción pendiente relevante.
2. BUSCA: Revisa la lista de TAREAS PENDIENTES YA ABIERTAS (en el contexto).
3. DECIDE: 
   - Si hay instrucción explícita de "crear" acción nueva → `generar_tarea_inmediata`.
   - Si es solo ajuste de fecha/hora de acción pendiente → `actualizar_tarea_existente`.
   - Si no existe tarea abierta relacionada → `generar_tarea_inmediata`.

DECISIÓN DE ACCIÓN:
OPCIÓN A: `generar_tarea_inmediata` (Lead requiere atención humana, no hay tarea abierta con mismo objetivo).
OPCIÓN B: `ia_puede_continuar` (Cliente solo saludó o confirmó dato simple, bot puede manejarlo).
OPCIÓN C: `marcar_sin_accion` (Lead dijo STOP, no llamar, o spam).
OPCIÓN D: `actualizar_tarea_existente` (Ya existe tarea abierta con el mismo objetivo, solo se ajusta fecha/dato).
```

## app/realtor/pending_reviews/tools/__init__.py

```py
"""
Tools de pending_reviews. Cada módulo es autocontenido:
- get_schema(tareas_pendientes_ghl) -> dict de function-calling
- PROMPT_FRAGMENT -> texto que describe la opción al modelo
- execute_handler(result, ctx) -> ejecuta la acción (GHL / webhook)

reasoning.py agrega los schemas y fragmentos de todos los módulos de aquí
para armar la lista de tools y el prompt. tasks.py despacha a
execute_handler() según la 'accion' elegida por el LLM.
"""
```

## app/realtor/pending_reviews/tools/actualizar_tarea_existente.py

```py
"""Tool y handler: actualizar una tarea existente en GHL."""
import logging
from datetime import datetime

import httpx

from app.realtor.shared.schemas import (
    RAZONAMIENTO_SCHEMA,
    TEMPERATURA_SCHEMA,
    NOTA_BITACORA_SCHEMA,
    HITO_RESUELTO_SCHEMA,
    DIAS_OFFSET_SCHEMA,
    HORAS_OFFSET_SCHEMA,
)
from app.realtor.shared.scheduling import MIAMI_TZ, aplicar_piso_temperatura, calcular_due_date
from app.realtor.shared.context import ExecutionContext

logger = logging.getLogger(__name__)

NOMBRE_TOOL = "actualizar_tarea_existente"
GHL_BASE_URL = "https://services.leadconnectorhq.com"


def get_schema(tareas_pendientes_ghl: list[dict]) -> dict:
    ids_validos = [t.get("id") for t in tareas_pendientes_ghl if t.get("id")]
    return {
        "type": "function",
        "function": {
            "name": NOMBRE_TOOL,
            "description": (
                "Úsala SOLO si la nueva solicitud tiene el EXACTO MISMO PROPÓSITO "
                "que una tarea ya abierta. Si el motivo o la acción son distintos, "
                "usa 'generar_tarea_inmediata'."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "razonamiento": RAZONAMIENTO_SCHEMA,
                    "temperatura": TEMPERATURA_SCHEMA,
                    "id_tarea_a_actualizar": {
                        "type": "string",
                        "enum": ids_validos,
                        "description": "ID exacto de la tarea a actualizar. NUNCA inventes uno.",
                    },
                    "nuevo_titulo": {
                        "type": "string",
                        "description": "Nuevo título de la tarea, o string vacío '' si el actual sigue siendo válido.",
                    },
                    "nueva_instruccion": {
                        "type": "string",
                        "description": "Nueva instrucción para el agente, o string vacío '' si la actual sigue siendo válida.",
                    },
                    "reagendar": {
                        "type": "boolean",
                        "description": "true si hay que cambiar cuándo se ejecuta la tarea. false si solo cambia título/instrucción o nada.",
                    },
                    "nuevos_dias_para_vencer": DIAS_OFFSET_SCHEMA,
                    "nuevas_horas_para_vencer": HORAS_OFFSET_SCHEMA,
                    "nota_bitacora": NOTA_BITACORA_SCHEMA,
                    "hito_resuelto": HITO_RESUELTO_SCHEMA,
                },
                "required": [
                    "razonamiento", "temperatura", "id_tarea_a_actualizar",
                    "nuevo_titulo", "nueva_instruccion", "reagendar",
                    "nuevos_dias_para_vencer", "nuevas_horas_para_vencer", "nota_bitacora",
                ],
            },
        },
    }


PROMPT_FRAGMENT = """
OPCIÓN D: `actualizar_tarea_existente`
Úsala cuando ya existe una tarea abierta (ver TAREAS PENDIENTES YA ABIERTAS arriba) cuyo objetivo sigue siendo el mismo — por ejemplo, el cliente pidió reagendar el contacto para otro momento, no respondió y toca reintentar más tarde, o dio un dato nuevo que ajusta la tarea sin cambiar su propósito de fondo. NO crees una tarea nueva ni elimines la anterior en estos casos.
- Debes indicar `id_tarea_a_actualizar` con el ID EXACTO de la tarea a modificar, tomado de la lista de TAREAS PENDIENTES YA ABIERTAS. Nunca inventes un ID.
- Si además cambió el objetivo o el mensaje a enviar, completa también `nuevo_titulo` y/o `nueva_instruccion` (respetando REGLA DE TÍTULOS DE TAREA y las REGLAS DE ORO); si no cambian, déjalos como string vacío "".
- Indica `reagendar` = true si corresponde cambiar cuándo se ejecuta la tarea. Si es true, completa `nuevos_dias_para_vencer` y `nuevas_horas_para_vencer` con el mismo criterio de la REGLA DE FECHA Y HORA DE LA TAREA y la REGLA DE PLAZO MÍNIMO SEGÚN TEMPERATURA. Si es false, completa igual ambos campos con cualquier valor válido (no se usarán) — SALVO que la tarea ya esté vencida (su fecha de vencimiento actual ya pasó respecto a HORA ACTUAL): en ese caso `reagendar` DEBE ser true, sin excepción, ya que una tarea no puede quedar con vencimiento en el pasado.
- Debes asignar `temperatura` igual que en las demás opciones.
"""


def _tarea_esta_vencida(tareas_pendientes_ghl: list[dict], ghl_task_id: str, ahora_miami: datetime) -> bool:
    """Chequea si la tarea a actualizar ya venció, comparando su fecha_limite actual contra 'ahora'."""
    tarea = next((t for t in tareas_pendientes_ghl if str(t.get("id")) == str(ghl_task_id)), None)
    if not tarea:
        return False
    fecha_raw = tarea.get("fecha_limite")
    if not fecha_raw:
        return False
    try:
        fecha_limite = datetime.fromisoformat(fecha_raw)
        if fecha_limite.tzinfo is None:
            fecha_limite = fecha_limite.replace(tzinfo=MIAMI_TZ)
        return fecha_limite < ahora_miami
    except (TypeError, ValueError) as e:
        logger.debug(f"No se pudo parsear fecha_limite={fecha_raw!r} de la tarea {ghl_task_id}: {e}")
        return False


def execute_handler(result: dict, ctx: ExecutionContext) -> dict:
    """
    PASO A: actualiza la tarea en GHL vía API directa (PUT).
    PASO B: dispara el webhook para actualizar los custom fields del contacto.
    """
    ghl_task_id = str(result.get("id_tarea_a_actualizar", ""))

    if not ctx.ghl_token:
        logger.error("❌ CRÍTICO: ghl_token es None o vacío. No se puede actualizar la tarea en GHL.")
        raise ValueError("GHL_API_KEY no configurada en el entorno del worker")

    # --- Seguro contra tareas vencidas ---------------------------------
    # No confiamos en que el LLM haya marcado reagendar=true cuando
    # correspondía: si la fecha de vencimiento actual de la tarea ya pasó,
    # forzamos el reagendado igual, server-side.
    reagendar = result.get("reagendar") is True
    if not reagendar and _tarea_esta_vencida(ctx.tareas_pendientes_ghl, ghl_task_id, ctx.ahora_miami):
        logger.warning(
            f"⚠️ Tarea {ghl_task_id} ya estaba vencida y el LLM no marcó reagendar=true. "
            f"Forzando reagendado | {ctx.contact_name}"
        )
        reagendar = True
    # ---------------------------------------------------------------------

    logger.info(f"✏️ [PASO A] Actualizando tarea {ghl_task_id} en GHL vía API directa | {ctx.contact_name}")

    payload_api_tarea = {
        "title": str(result.get("nuevo_titulo", "")),
        "body": str(result.get("nueva_instruccion", "")),
    }
    # Si el LLM dejó título/instrucción en "" (= "sin cambios" según el
    # prompt), no lo mandamos: evita pisar el título/descripción actual en
    # GHL con un valor vacío.
    payload_api_tarea = {k: v for k, v in payload_api_tarea.items() if v}

    nueva_fecha_limite_para_webhook = ""
    if reagendar:
        dias = aplicar_piso_temperatura(
            int(result.get("nuevos_dias_para_vencer", 0)),
            result.get("temperatura", "Frio"),
            ctx.contact_name,
            campo="nuevos_dias_para_vencer",
        )
        try:
            due_date_iso = calcular_due_date(
                dias_para_vencer=dias,
                horas_para_vencer=int(result.get("nuevas_horas_para_vencer", 0)),
                ahora_miami=ctx.ahora_miami,
            )
            payload_api_tarea["dueDate"] = due_date_iso
            nueva_fecha_limite_para_webhook = due_date_iso
        except (TypeError, ValueError) as e:
            logger.error(f"❌ Error calculando dueDate para API | {ctx.contact_name}: {e}")

    with httpx.Client() as client:
        response_tarea = client.put(
            f"{GHL_BASE_URL}/contacts/{ctx.contact_id}/tasks/{ghl_task_id}",
            headers={
                "Authorization": f"Bearer {ctx.ghl_token}",
                "Version": "2021-07-28",
                "Content-Type": "application/json",
            },
            json=payload_api_tarea,
            timeout=15.0,
        )
        response_tarea.raise_for_status()
        logger.info(f"✅ Tarea {ghl_task_id} actualizada exitosamente en GHL | {ctx.contact_name}")

    webhook_payload = {
        "task_id": ctx.task_id,
        "contact_id": ctx.contact_id,
        "contact_name": ctx.contact_name,
        "accion": "actualizar_tarea",
        "ghl_task_id": ghl_task_id,
        "temperatura": result.get("temperatura", "No determinado"),
        "task_title": result.get("nuevo_titulo") or "",
        "task_body": result.get("nueva_instruccion") or "",
        "nueva_fecha_limite": nueva_fecha_limite_para_webhook,
        "razonamiento": result.get("razonamiento", ""),
        "nota_bitacora": ctx.nota_bitacora,
        "custom_field_value": ctx.fecha_hora_revision,
    }

    logger.info(f"🚀 [PASO B] Disparando webhook para actualizar custom fields | {ctx.contact_name}")
    webhook_response = httpx.post(ctx.webhook_url, json=webhook_payload, timeout=15.0)
    webhook_response.raise_for_status()
    resp_json = webhook_response.json()
    trigger_id = resp_json.get("triggerId") or resp_json.get("id") or "N/A"
    logger.info(f"✅ Webhook OK (custom fields actualizados) | {ctx.contact_name} | trigger_id={trigger_id}")

    return {"status": "success", "ghl_task_id": ghl_task_id, "trigger_id": trigger_id}
```

## app/realtor/pending_reviews/tools/generar_tarea_inmediata.py

```py
"""Tool y handler: crear una tarea NUEVA en GHL (vía webhook)."""
import logging

import httpx

from app.realtor.shared.schemas import (
    RAZONAMIENTO_SCHEMA,
    TEMPERATURA_SCHEMA,
    NOTA_BITACORA_SCHEMA,
    HITO_RESUELTO_SCHEMA,
    DIAS_OFFSET_SCHEMA,
    HORAS_OFFSET_SCHEMA,
)
from app.realtor.shared.scheduling import aplicar_piso_temperatura, formatear_due_date_ghl_webhook
from app.realtor.shared.context import ExecutionContext

logger = logging.getLogger(__name__)

NOMBRE_TOOL = "generar_tarea_inmediata"


def get_schema(tareas_pendientes_ghl: list[dict]) -> dict:
    return {
        "type": "function",
        "function": {
            "name": NOMBRE_TOOL,
            "description": "El lead requiere atención humana inmediata. Genera una tarea NUEVA.",
            "parameters": {
                "type": "object",
                "properties": {
                    "razonamiento": RAZONAMIENTO_SCHEMA,
                    "temperatura": TEMPERATURA_SCHEMA,
                    "interes": {"type": "string", "enum": ["Casa", "Townhouse", "Inversión", "Rentar", "No determinado"]},
                    "calificacion_lead": {"type": "integer", "minimum": 1, "maximum": 10},
                    "titulo_tarea": {"type": "string", "description": "Título corto y accionable (SIN fechas absolutas)."},
                    "instruccion_para_agente": {"type": "string", "description": "Instrucción detallada. Si incluye un mensaje para enviar, redáctalo aquí aplicando las reglas de oro."},
                    "dias_para_vencer": DIAS_OFFSET_SCHEMA,
                    "horas_para_vencer": HORAS_OFFSET_SCHEMA,
                    "nota_bitacora": NOTA_BITACORA_SCHEMA,
                    "hito_resuelto": HITO_RESUELTO_SCHEMA,
                },
                "required": [
                    "razonamiento", "temperatura", "interes", "calificacion_lead",
                    "titulo_tarea", "instruccion_para_agente", "dias_para_vencer",
                    "horas_para_vencer", "nota_bitacora",
                ],
            },
        },
    }


PROMPT_FRAGMENT = """
OPCIÓN A: `generar_tarea_inmediata`
Úsala si el lead requiere atención humana (pregunta compleja, pidió precio, mostró interés, o hay que aplicar un seguimiento de los 14 escenarios) Y no existe ya una tarea abierta con el mismo objetivo (si existe, usa OPCIÓN D en su lugar).
- Debes proporcionar un `titulo_tarea` claro y corto (ver REGLA DE TÍTULOS DE TAREA).
- Debes proporcionar `instruccion_para_agente`: Instrucciones precisas de qué hacer. Si requiere enviar un mensaje, incluye el borrador exacto del mensaje aplicando las REGLAS DE ORO (corto, humano, pidiendo correo, sin dirección exacta).
- Debes indicar `dias_para_vencer` y `horas_para_vencer` según la REGLA DE FECHA Y HORA DE LA TAREA y la REGLA DE PLAZO MÍNIMO SEGÚN TEMPERATURA.
- Debes asignar `temperatura` (Caliente / Tibio / Frio / No interesado) según la intención de compra mostrada en la última interacción relevante.
"""


def execute_handler(result: dict, ctx: ExecutionContext) -> dict:
    """Construye el webhook_payload para crear una tarea nueva y lo dispara."""
    dias_para_vencer = aplicar_piso_temperatura(
        int(result.get("dias_para_vencer", 0)),
        result.get("temperatura", "Frio"),
        ctx.contact_name,
    )

    try:
        nueva_fecha_limite = formatear_due_date_ghl_webhook(
            dias_para_vencer=dias_para_vencer,
            horas_para_vencer=int(result.get("horas_para_vencer", 0)),
            ahora_miami=ctx.ahora_miami,
        )
    except (TypeError, ValueError) as e:
        logger.error(f"❌ Error calculando fecha para webhook | {ctx.contact_name}: {e}")
        nueva_fecha_limite = ""

    webhook_payload = {
        "task_id": ctx.task_id,
        "contact_id": ctx.contact_id,
        "contact_name": ctx.contact_name,
        "accion": "crear_tarea",
        "temperatura": result.get("temperatura", "No determinado"),
        "interes": result.get("interes", "No determinado"),
        "calificacion_lead": int(result.get("calificacion_lead", 5)),
        "task_title": str(result.get("titulo_tarea", "")),
        "task_body": str(result.get("instruccion_para_agente", "")),
        "razonamiento": str(result.get("razonamiento", "")),
        "nota_bitacora": ctx.nota_bitacora,
        "custom_field_value": ctx.fecha_hora_revision,
        "nueva_fecha_limite": nueva_fecha_limite,
    }

    logger.info(f"🚀 Disparando webhook | accion={NOMBRE_TOOL} | {ctx.contact_name}")
    response = httpx.post(ctx.webhook_url, json=webhook_payload, timeout=15.0)
    response.raise_for_status()
    resp_json = response.json()
    trigger_id = resp_json.get("triggerId") or resp_json.get("id") or "N/A"
    logger.info(f"✅ Webhook OK | {ctx.contact_name} | trigger_id={trigger_id}")

    return {"status": "success", "trigger_id": trigger_id}
```

## app/realtor/pending_reviews/tools/ia_puede_continuar.py

```py
"""Tool y handler: el bot puede seguir la conversación sin intervención humana inmediata."""
import logging

import httpx

from app.realtor.shared.schemas import (
    RAZONAMIENTO_SCHEMA,
    TEMPERATURA_SCHEMA,
    NOTA_BITACORA_SCHEMA,
    HITO_RESUELTO_SCHEMA,
)
from app.realtor.shared.context import ExecutionContext

logger = logging.getLogger(__name__)

NOMBRE_TOOL = "ia_puede_continuar"


def get_schema(tareas_pendientes_ghl: list[dict]) -> dict:
    return {
        "type": "function",
        "function": {
            "name": NOMBRE_TOOL,
            "description": "El mensaje del cliente es simple y el bot puede manejarlo sin intervención humana inmediata.",
            "parameters": {
                "type": "object",
                "properties": {
                    "razonamiento": RAZONAMIENTO_SCHEMA,
                    "temperatura": TEMPERATURA_SCHEMA,
                    "titulo_tarea": {
                        "type": "string",
                        "description": "Título corto de una sugerencia de seguimiento OPCIONAL (ej: '[Sugerencia] Intentar obtener correo'). String vacío '' si no aplica.",
                    },
                    "nota_bitacora": NOTA_BITACORA_SCHEMA,
                    "hito_resuelto": HITO_RESUELTO_SCHEMA,
                },
                "required": ["razonamiento", "temperatura", "titulo_tarea", "nota_bitacora"],
            },
        },
    }


PROMPT_FRAGMENT = """
OPCIÓN B: `ia_puede_continuar`
Úsala si el cliente solo saludó, confirmó un dato simple (ej: "sí, gracias") o hizo una pregunta que el bot ya puede responder automáticamente sin intervención humana.
- Debes asignar `temperatura` (Caliente / Tibio / Frio / No interesado) igual que en la OPCIÓN A, según la intención de compra mostrada hasta el momento.
- Aunque no se requiera intervención humana ahora, revisa si hay una oportunidad de seguimiento relevante (correo aún no capturado, cita por confirmar, llamada pendiente, recontactar en unos días, etc.) y complétala en `titulo_tarea` con formato "[Sugerencia] <acción concreta>", o deja `titulo_tarea` vacío si no aplica ninguna.
- En `razonamiento`, además de explicar por qué no se necesita intervención inmediata, justifica brevemente la temperatura asignada y, si generaste una sugerencia en `titulo_tarea`, explica por qué esa es la sugerencia pertinente en este momento.
"""


def execute_handler(result: dict, ctx: ExecutionContext) -> dict:
    webhook_payload = {
        "task_id": ctx.task_id,
        "contact_id": ctx.contact_id,
        "contact_name": ctx.contact_name,
        "accion": "ia_continua",
        "temperatura": result.get("temperatura", "No determinado"),
        "task_title": str(result.get("titulo_tarea", "")),
        "razonamiento": str(result.get("razonamiento", "")),
        "nota_bitacora": ctx.nota_bitacora,
        "custom_field_value": ctx.fecha_hora_revision,
    }

    logger.info(f"🚀 Disparando webhook | accion={NOMBRE_TOOL} | {ctx.contact_name}")
    response = httpx.post(ctx.webhook_url, json=webhook_payload, timeout=15.0)
    response.raise_for_status()
    resp_json = response.json()
    trigger_id = resp_json.get("triggerId") or resp_json.get("id") or "N/A"
    logger.info(f"✅ Webhook OK | {ctx.contact_name} | trigger_id={trigger_id}")

    return {"status": "success", "trigger_id": trigger_id}
```

## app/realtor/pending_reviews/tools/marcar_sin_accion.py

```py
"""Tool y handler: lead pidió STOP / no contacto / es spam. No se crea tarea."""
import logging

import httpx

from app.realtor.shared.schemas import (
    RAZONAMIENTO_SCHEMA,
    TEMPERATURA_SCHEMA,
    NOTA_BITACORA_SCHEMA,
    HITO_RESUELTO_SCHEMA,
)
from app.realtor.shared.context import ExecutionContext

logger = logging.getLogger(__name__)

NOMBRE_TOOL = "marcar_sin_accion"


def get_schema(tareas_pendientes_ghl: list[dict]) -> dict:
    return {
        "type": "function",
        "function": {
            "name": NOMBRE_TOOL,
            "description": "El lead dijo STOP, no le llamen, o es spam. No se debe crear tarea ni contactar.",
            "parameters": {
                "type": "object",
                "properties": {
                    "motivo": {"type": "string", "description": "Motivo del descarte."},
                    "razonamiento": RAZONAMIENTO_SCHEMA,
                    "temperatura": TEMPERATURA_SCHEMA,
                    "nota_bitacora": NOTA_BITACORA_SCHEMA,
                    "hito_resuelto": HITO_RESUELTO_SCHEMA,
                },
                "required": ["motivo", "razonamiento", "temperatura", "nota_bitacora"],
            },
        },
    }


PROMPT_FRAGMENT = """
OPCIÓN C: `marcar_sin_accion`
Úsala SOLO si el lead dijo explícitamente "STOP", "no me llamen", "ya compré y no quiero referir", o es spam evidente.
- Asigna siempre `temperatura` = "No interesado" en este caso.
"""


def execute_handler(result: dict, ctx: ExecutionContext) -> dict:
    webhook_payload = {
        "task_id": ctx.task_id,
        "contact_id": ctx.contact_id,
        "contact_name": ctx.contact_name,
        "accion": "descartar",
        "temperatura": "No interesado",
        "task_title": "",
        "motivo": str(result.get("motivo", "")),
        "razonamiento": str(result.get("razonamiento", "")),
        "nota_bitacora": ctx.nota_bitacora,
        "custom_field_value": ctx.fecha_hora_revision,
    }

    logger.info(f"🚀 Disparando webhook | accion={NOMBRE_TOOL} | {ctx.contact_name}")
    response = httpx.post(ctx.webhook_url, json=webhook_payload, timeout=15.0)
    response.raise_for_status()
    resp_json = response.json()
    trigger_id = resp_json.get("triggerId") or resp_json.get("id") or "N/A"
    logger.info(f"✅ Webhook OK | {ctx.contact_name} | trigger_id={trigger_id}")

    return {"status": "success", "trigger_id": trigger_id}
```

## app/realtor/pending_reviews/reasoning.py

```py
import json
import logging
import os
import re
from datetime import datetime
from functools import lru_cache
from pathlib import Path

from app.llm_client import get_openrouter_client
from app.realtor.shared.scheduling import MIAMI_TZ, formatear_fecha_hoy
from app.realtor.pending_reviews.tools import (
    generar_tarea_inmediata,
    ia_puede_continuar,
    marcar_sin_accion,
    actualizar_tarea_existente,
)

logger = logging.getLogger(__name__)

PROMPT_STATIC_PATH = Path(__file__).resolve().parents[2] / "contextos" / "realtor" / "prompt_pending_reviews_static.txt"

MARCADOR_OPCIONES = "<<OPCIONES_DE_ACCION>>"

# Orden en que se presentan las opciones al modelo (A, B, C, D). El texto de
# cada una vive junto a su tool, en PROMPT_FRAGMENT — reasoning.py solo las
# agrega, no las redefine.
TOOL_MODULES = [
    generar_tarea_inmediata,
    ia_puede_continuar,
    marcar_sin_accion,
    actualizar_tarea_existente,
]


def _formatear_tareas_pendientes(tareas: list[dict]) -> str:
    """Convierte el array de tareas pendientes de GHL en un bloque de texto legible."""
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


@lru_cache(maxsize=1)
def _get_static_prompt() -> str:
    """
    Carga la plantilla estática una sola vez y la arma insertando el
    PROMPT_FRAGMENT de cada tool en el marcador <<OPCIONES_DE_ACCION>>.

    El resultado es 100% estático e idéntico en TODAS las llamadas —
    incluye las 4 opciones sin importar si el contacto tiene tareas
    pendientes o no — para maximizar el hit rate del prompt caching
    (cache_control ephemeral). Si se condicionara la inclusión de la
    OPCIÓN D según haya o no tareas abiertas, se perdería cache en cada
    contacto sin tareas pendientes.
    """
    plantilla = PROMPT_STATIC_PATH.read_text(encoding="utf-8")
    fragmentos = "\n".join(modulo.PROMPT_FRAGMENT for modulo in TOOL_MODULES)
    return plantilla.replace(MARCADOR_OPCIONES, fragmentos)


def _build_tools(tareas_pendientes_ghl: list[dict]) -> list[dict]:
    """
    Arma la lista de tools para esta llamada. 'actualizar_tarea_existente'
    solo se agrega si hay al menos una tarea abierta con ID válido — de lo
    contrario su 'enum' de IDs quedaría vacío y el schema sería inválido.
    """
    tools = [
        generar_tarea_inmediata.get_schema(tareas_pendientes_ghl),
        ia_puede_continuar.get_schema(tareas_pendientes_ghl),
        marcar_sin_accion.get_schema(tareas_pendientes_ghl),
    ]
    ids_validos = [t.get("id") for t in tareas_pendientes_ghl if t.get("id")]
    if ids_validos:
        tools.append(actualizar_tarea_existente.get_schema(tareas_pendientes_ghl))
    return tools


def analyze_pending_review(
    contact_name: str,
    historial_texto: str,
    tareas_pendientes_ghl: list[dict] | None = None,
    señal_cierre: str = "seguir",  # <-- NUEVO PARÁMETRO
    max_intentos: int = 2,
) -> dict:
    
    tareas_pendientes_ghl = tareas_pendientes_ghl or []

    ahora_miami = datetime.now(MIAMI_TZ)
    fecha_hoy_str = formatear_fecha_hoy(ahora_miami)
    hora_actual_str = ahora_miami.strftime("%H:%M")

    # BLOQUE A: Estático, cacheable (reglas, formatos, opciones de las tools)
    bloque_estatico = _get_static_prompt()

    bloque_variable = (
        f"--- CONTEXTO DE LA EJECUCIÓN ---\n"
        f"HOY ES: {fecha_hoy_str}\n"
        f"HORA ACTUAL (Miami): {hora_actual_str}\n\n"
        f"SEÑAL DE CIERRE DE VENTANA: {señal_cierre.upper()}\n"
        f"(Instrucción: Si es 'EVALUAR', determina si el hito está resuelto. Si es 'SEGUIR' o 'CERRAR', no evalúes el hito).\n\n"
        f"TAREAS PENDIENTES YA ABIERTAS PARA ESTE CONTACTO EN GHL:\n"
        f"{_formatear_tareas_pendientes(tareas_pendientes_ghl)}\n\n"
        f"Contacto: {contact_name}\n\n"
        f"HISTORIAL COMPLETO DE CONVERSACIÓN (Incluye bitácora fija + ventana de mensajes nuevos):\n"
        f"{historial_texto}"
    )

    tools = _build_tools(tareas_pendientes_ghl)
    client = get_openrouter_client()

    model_name = os.environ.get("PENDING_REVIEWS_MODEL") or os.environ.get("COOL_LEADS_MODEL", "anthropic/claude-sonnet-5")
    temperature = float(os.environ.get("PENDING_REVIEWS_TEMPERATURE") or os.environ.get("COOL_LEADS_TEMPERATURE", "0.3"))
    max_tokens = int(os.environ.get("PENDING_REVIEWS_MAX_TOKENS") or os.environ.get("COOL_LEADS_MAX_TOKENS", "1800"))

    # Estructura de mensaje con Prompt Caching (Fase 0)
    messages = [{
        "role": "user",
        "content": [
            {
                "type": "text",
                "text": bloque_estatico,
                "cache_control": {"type": "ephemeral"},
            },
            {
                "type": "text",
                "text": bloque_variable,
            }
        ]
    }]

    tokens_actuales = max_tokens
    ultimo_error: json.JSONDecodeError | None = None

    for intento in range(1, max_intentos + 1):
        response = client.chat.completions.create(
            model=model_name,
            temperature=temperature,
            max_tokens=tokens_actuales,
            messages=messages,
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
            logger.debug(f"📄 Argumentos crudos recibidos:\n{raw_arguments}")

            if intento < max_intentos:
                # Modificamos SOLO el bloque variable para el reintento.
                # Esto preserva el caché del bloque estático (ephemeral).
                if fue_truncado:
                    tokens_actuales = int(tokens_actuales * 1.5)
                    messages[0]["content"][1]["text"] = (
                        f"{bloque_variable}\n\n"
                        f"⚠️ ADVERTENCIA: tu respuesta anterior se cortó antes de completar el JSON "
                        f"(te quedaste sin espacio). En 'nota_bitacora', resume los hitos más antiguos "
                        f"en un solo renglón y detalla solo los últimos 6-8. Sé conciso y cierra el JSON."
                    )
                    logger.warning(f"⚠️ Truncamiento detectado, reintentando con max_tokens={tokens_actuales} | {contact_name}")
                else:
                    messages[0]["content"][1]["text"] = (
                        f"{bloque_variable}\n\n"
                        f"⚠️ ADVERTENCIA: tu respuesta anterior tenía JSON inválido (error: {e}). "
                        f"La causa más común es una comilla doble (\") sin escapar. Genera de nuevo "
                        f"la respuesta completa, revisando que NINGÚN campo de texto contenga comillas dobles sin escapar."
                    )
                continue

            raise RuntimeError(
                f"El modelo devolvió JSON inválido en los argumentos de la herramienta "
                f"tras {max_intentos} intentos (finish_reason={finish_reason}). Error: {e}"
            )

        accion = tool_call.function.name
        logger.info(f"✅ Decisión: {accion} | {contact_name} | intento {intento}/{max_intentos}")

        # Monitoreo de efectividad del caché
        usage = getattr(response, "usage", None)
        if usage:
            cache_read = getattr(usage, "cache_read_input_tokens", 0)
            if not cache_read:
                prompt_details = getattr(usage, "prompt_tokens_details", None)
                if prompt_details is not None:
                    cache_read = getattr(prompt_details, "cached_tokens", 0) or 0

            if cache_read > 0:
                logger.info(f"💾 Cache HIT: {cache_read} tokens leídos de caché para {contact_name}")
            else:
                logger.debug(f"📝 Cache MISS (primera vez o TTL expirado) para {contact_name}")

        return {"accion": accion, **args}

    raise RuntimeError(f"Fallo inesperado analizando a {contact_name}. Último error: {ultimo_error}")
```

