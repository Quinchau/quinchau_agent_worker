# app/templates.py
"""
Plantillas de respuesta para el worker de Quinchau Motos.
Las plantillas usan formato de string con {variables}.
"""

TEMPLATES = {
    "intencion_compra_repuestos": {
        "found": "{url}",
        "not_found": "No encontré un producto para tu solicitud, pero aquí te dejo el catálogo del modelo {modelo} para que lo explores: {url}"
    },
    "consulta_disponibilidad": {
        "found": "{url}",
        "not_found": "No encontré disponibilidad específica, pero puedes revisar el catálogo del modelo {modelo}: {url}"
    }
}

def get_template(intencion: str, mode: str = "found") -> str:
    """
    Obtiene una plantilla con validación de existencia.
    
    Args:
        intencion: Nombre de la intención (ej. 'intencion_compra_repuestos')
        mode: Tipo de plantilla ('found' o 'not_found')
    
    Returns:
        String de plantilla
    
    Raises:
        KeyError: Si la intención o el modo no existen
    """
    if intencion not in TEMPLATES:
        raise KeyError(f"No template defined for intencion: {intencion}")
    if mode not in TEMPLATES[intencion]:
        raise KeyError(f"No {mode} template for intencion: {intencion}")
    return TEMPLATES[intencion][mode]