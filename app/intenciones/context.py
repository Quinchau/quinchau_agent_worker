"""
Contexto compartido y registro de manejadores de intención.

Cada manejador recibe un único objeto `IntentContext` en vez de una lista
larga de parámetros posicionales, y se registra a sí mismo con el
decorador `@registrar("nombre_intencion")`.
"""
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from openai import OpenAI


@dataclass
class IntentContext:
    """Todo lo que un manejador de intención necesita para operar."""

    message: str
    contact_id: str
    channel: str
    first_name: str
    last_name: str

    intencion: str
    confianza: float
    entidades_detectadas: Dict[str, Any]
    razon: str

    state: Dict[str, Any]
    state_manager: Any  # AgentStateManager
    client: OpenAI

    historial_texto: str = ""
    resolution: Dict[str, Any] = field(default_factory=dict)


# Un manejador recibe el contexto y devuelve el dict de respuesta que se
# retorna al llamador de process_ghl_message. Si devuelve None, tasks.py
# interpreta que el manejador prefiere delegar en el LLM genérico
# (ver intenciones/compra.py para un ejemplo real de este caso).
HandlerFn = Callable[[IntentContext], Optional[Dict[str, Any]]]

_REGISTRO: Dict[str, HandlerFn] = {}


def registrar(nombre: str) -> Callable[[HandlerFn], HandlerFn]:
    """Decorador: registra un manejador bajo el nombre de una intención."""

    def _decorator(fn: HandlerFn) -> HandlerFn:
        _REGISTRO[nombre] = fn
        return fn

    return _decorator


def obtener_manejador(nombre: str) -> Optional[HandlerFn]:
    return _REGISTRO.get(nombre)


def intenciones_registradas() -> List[str]:
    return list(_REGISTRO.keys())