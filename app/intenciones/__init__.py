"""
Manejadores de intención.

Cada módulo de este paquete se registra a sí mismo (vía @registrar) al ser
importado. Este __init__ se encarga de importarlos a todos para que el
registro quede poblado apenas se importe `intenciones`.

Para agregar una intención nueva:
  1. crear `intenciones/mi_intencion.py`
  2. definir `def handle(ctx: IntentContext) -> dict:` decorado con
     `@registrar("nombre_exacto_de_la_intencion")`
  3. agregar el import acá abajo

No hace falta tocar tasks.py.
"""
from .context import IntentContext, obtener_manejador, registrar, intenciones_registradas  # noqa: F401

from . import sin_clasificar  # noqa: F401,E402
from . import envios_y_entregas  # noqa: F401,E402
from . import retiro_y_pago_personal  # noqa: F401,E402
from . import envio_por_delivery  # noqa: F401,E402
from . import saludo  # noqa: F401,E402
from . import compra_al_mayoreo  # noqa: F401,E402
from . import ubicacion_horario  # noqa: F401,E402
from . import orden_sin_despacho  # noqa: F401,E402
from . import compra  # noqa: F401,E402

__all__ = [
    "IntentContext",
    "obtener_manejador",
    "registrar",
    "intenciones_registradas",
]