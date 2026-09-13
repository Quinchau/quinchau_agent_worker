"""
Tools de pending_reviews. Cada módulo es autocontenido:
- get_schema(tareas_pendientes_ghl) -> dict de function-calling
- PROMPT_FRAGMENT -> texto que describe la opción al modelo
- execute_handler(result, ctx) -> ejecuta la acción (GHL / webhook)

reasoning.py agrega los schemas y fragmentos de todos los módulos de aquí
para armar la lista de tools y el prompt. tasks.py despacha a
execute_handler() según la 'accion' elegida por el LLM.
"""