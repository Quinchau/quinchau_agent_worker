# test_validar.py
import os, sys
sys.path.insert(0, '.')
os.environ.setdefault("OPENROUTER_API_KEY", "dummy")

from app.tasks import _validar_termino

casos = [
    # (input, tipo, resultado_esperado)
    ("HJ125S",   "modelo",   "HJ125S"),    # canónico exacto
    ("hj125s",   "modelo",   "HJ125S"),    # lowercase → debe normalizar
    ("DR 650",   "modelo",   "DR 650"),    # con espacio
    ("dr 650",   "modelo",   "DR 650"),    # lowercase con espacio
    ("suichera", "producto", "Suichera"),  # producto simple
    ("SUICHERA", "producto", "Suichera"),  # uppercase
    ("xyz_inventado_123", "modelo", None), # no existe → None
    (None,       "modelo",   None),        # None input → None
]

print(f"{'Input':<25} {'Tipo':<10} {'Esperado':<20} {'Obtenido':<20} {'OK'}")
print("-" * 85)
for texto, tipo, esperado in casos:
    obtenido = _validar_termino(texto, tipo)
    ok = "✅" if obtenido == esperado else "❌"
    print(f"{str(texto):<25} {tipo:<10} {str(esperado):<20} {str(obtenido):<20} {ok}")