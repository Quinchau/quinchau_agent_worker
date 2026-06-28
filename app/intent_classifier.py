# app/intent_classifier.py
import logging
from .catalog_cache import CatalogCache

logger = logging.getLogger(__name__)

class IntentClassifier:
    def __init__(self):
        self.cache = CatalogCache()

    def get_entidades_bloqueantes(self, intencion):
        """
        Obtiene las entidades bloqueantes para una intención, desde cache (Redis,
        TTL 5 min) con fallback a BD si Redis no está disponible.
        """
        if not intencion:
            logger.warning("⚠️ intencion vacía en get_entidades_bloqueantes")
            return []

        bloqueantes_map = self.cache.get_bloqueantes_map()
        return bloqueantes_map.get(intencion, [])

    def validate_entities(self, intencion, state):
        if not intencion:
            logger.warning("⚠️ intencion vacía en validate_entities")
            return []

        if not state or not isinstance(state, dict):
            logger.warning("⚠️ state inválido en validate_entities")
            return []

        bloqueantes = self.get_entidades_bloqueantes(intencion)

        if not bloqueantes:
            logger.info(f"ℹ️ No hay entidades bloqueantes para '{intencion}'")
            return []

        faltantes = []
        for entidad in bloqueantes:
            valor = state.get(entidad)
            if valor is None or valor == "":
                faltantes.append(entidad)
                logger.info(f"⚠️ Entidad faltante: {entidad}")

        if faltantes:
            logger.info(f"⚠️ Faltan {len(faltantes)} entidades para '{intencion}': {faltantes}")
        else:
            logger.info(f"✅ Todas las entidades resueltas para '{intencion}'")

        return faltantes