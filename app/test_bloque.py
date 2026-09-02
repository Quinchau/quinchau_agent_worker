# test_bloque.py (correr desde la raíz del proyecto)
import os, sys
sys.path.insert(0, '.')
os.environ.setdefault("OPENROUTER_API_KEY", "dummy")  # no hace llamadas LLM acá

from app.classifier_block_builder import get_bloque_estatico, invalidar_bloque_estatico

# Forzar rebuild desde BD/Redis
bloque = invalidar_bloque_estatico()

print(f"version      : {bloque['version']}")
print(f"modelos      : {bloque['total_modelos']}")
print(f"productos    : {bloque['total_productos']}")
print(f"built_at     : {bloque['built_at']}")
print()
print("--- primeras 5 líneas del diccionario ---")
print('\n'.join(bloque['diccionario_texto'].splitlines()[:10]))
print()
print("--- intenciones ---")
print(bloque['intenciones_texto'])