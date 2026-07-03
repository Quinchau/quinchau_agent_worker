"""
Carga de prompts desde contextos/<name>.txt.
"""
import os

PROMPTS_DIR = os.path.join(os.path.dirname(__file__), "contextos")


class _SafeDict(dict):
    def __missing__(self, key):
        return ""


def load_prompt(name: str, **kwargs) -> str:
    """
    Carga un prompt desde contextos/<name>.txt e inyecta variables con .format().
    Los campos opcionales que no se pasen se reemplazan por cadena vacía.
    """
    path = os.path.join(PROMPTS_DIR, f"{name}.txt")
    with open(path, "r", encoding="utf-8") as f:
        template = f.read()

    return template.format_map(_SafeDict(**kwargs))