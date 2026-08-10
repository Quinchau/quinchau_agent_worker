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
    variante_producto.py
  __init__.py
  agent_state.py
  agent.py
  catalog_cache.py
  database.py
  entity_resolver.py
  ghl.py
  jobs.py
  main.py
  models.py
  product_search_cache.py
  prompts.py
  redis_queue.py
  tasks.py
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
```



# Selected Files Content

## app/ghl.py

```py
import logging
import os
import time

import httpx

logger = logging.getLogger(__name__)


def send_message_to_ghl(contact_id: str, message: str, channel: str = "WhatsApp") -> dict:
    """
    Envía la respuesta generada por el LLM de vuelta al contacto en GHL
    usando la Conversations API (Send a new message).
    """
    token = os.getenv("GHL_PRIVATE_TOKEN")
    if not token:
        raise ValueError("GHL_PRIVATE_TOKEN no configurada")

    url = "https://services.leadconnectorhq.com/conversations/messages"
    headers = {
        "Authorization": f"Bearer {token}",
        "Version": "2021-04-15",
        "Content-Type": "application/json",
    }
    payload = {
        "type": channel,
        "contactId": contact_id,
        "message": message,
    }

    resp = httpx.post(url, json=payload, headers=headers, timeout=10)
    resp.raise_for_status()
    return resp.json()


def send_multiple_messages(contact_id, messages, channel, delay: float = 0.5) -> None:
    """Envía múltiples mensajes consecutivos a GHL."""
    for i, msg in enumerate(messages):
        if not msg or not msg.strip():
            continue
        send_message_to_ghl(contact_id, msg.strip(), channel)
        logger.info(f"📤 Mensaje {i+1}/{len(messages)} enviado: {msg[:50]}...")
        if i < len(messages) - 1:
            time.sleep(delay)
```

