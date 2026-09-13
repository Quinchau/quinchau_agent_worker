"""Contexto de ejecución compartido, pasado a execute_handler() de cada tool de pending_reviews."""
from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class ExecutionContext:
    """
    Todo lo que un execute_handler() puede necesitar para ejecutar su acción
    (llamar a GHL, disparar el webhook), sin tener que conocer nada del resto
    del pipeline de decisión.
    """
    task_id: str
    contact_id: str
    contact_name: str
    nota_bitacora: str
    fecha_hora_revision: str
    ahora_miami: datetime
    webhook_url: str
    tareas_pendientes_ghl: list[dict] = field(default_factory=list)
    ghl_token: str | None = None