# test_clasificador.py
import os, sys
sys.path.insert(0, '.')

from app.classifier import clasificar

casos = [
    {
        "label": "modelo + producto explícitos",
        "mensaje": "hola, tengo una HJ125S y necesito una suichera",
        "historial": "",
        "modelo_heredado": None,
    },
    {
        "label": "solo modelo, sin producto",
        "mensaje": "consulta para la DR 650",
        "historial": "",
        "modelo_heredado": None,
    },
    {
        "label": "producto con alias (sin modelo)",
        "mensaje": "tienen instalacion para alarma?",
        "historial": "",
        "modelo_heredado": None,
    },
    {
        "label": "modelo heredado + mensaje ambiguo",
        "mensaje": "cuánto sale?",
        "historial": "Usuario: necesito suichera para la hj125s\nAsistente: ¿cuál variante necesitás?",
        "modelo_heredado": "HJ125S",
    },
    {
        "label": "saludo sin intención de compra",
        "mensaje": "hola buenas",
        "historial": "",
        "modelo_heredado": None,
    },
]

print()
for c in casos:
    print(f"🧪 {c['label']}")
    resultado = clasificar(c['mensaje'], c['historial'], c['modelo_heredado'])
    print(f"   intención : {resultado['intencion']}")
    print(f"   modelo    : {resultado['modelo']}")
    print(f"   producto  : {resultado['producto']}")
    print(f"   error     : {resultado['error']}")
    print()