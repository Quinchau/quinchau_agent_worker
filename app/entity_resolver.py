# app/entity_resolver.py
import re
import logging
from .catalog_cache import CatalogCache
from .agent_state import AgentStateManager

logger = logging.getLogger(__name__)

class EntityResolver:
    def __init__(self):
        self.cache = CatalogCache()
        self.state_manager = AgentStateManager()
    
    def normalize_text(self, text):
        # Sin cambios
        if not text:
            return ""
        text = text.lower().strip()
        import unicodedata
        text = unicodedata.normalize('NFKD', text)
        text = ''.join([c for c in text if not unicodedata.combining(c)])
        return text

    def extract_entities(self, message):
        """
        Extrae entidades del mensaje contra la lista de términos+alias
        ya resuelta (merge+dedup+sort) que entrega el cache (Redis, TTL 5 min,
        fallback a BD si Redis no está disponible).
        """
        if not message:
            return []

        normalized = self.normalize_text(message)
        matches = []
        seen_terminos = set()

        all_patterns = self.cache.get_terminos_patterns()  # ✅ NUEVO — reemplaza las 2 queries + merge

        for item in all_patterns:
            pattern = item['pattern']
            termino = item['termino']

            if termino in seen_terminos:
                continue

            pattern_regex = r'\b' + re.escape(pattern) + r'\b'
            if re.search(pattern_regex, normalized):
                seen_terminos.add(termino)
                matches.append({
                    'termino_id': item['termino_id'],
                    'termino': termino,
                    'id_entidad': item['id_entidad'],
                    'entidad_nombre': item['entidad_nombre'],
                    'alias': pattern
                })
                logger.debug(f"✅ Match: '{pattern}' → término '{termino}'")

        logger.info(f"🔍 Encontrados {len(matches)} matches en mensaje")
        return matches

    # resolve_entities() no cambia — sigue operando igual sobre lo que devuelve
    # extract_entities(), sin importar de dónde salió la lista de patrones.
    
    def resolve_entities(self, message, contact_id):
        if not message or not contact_id:
            logger.warning("⚠️ message o contact_id vacío")
            return {'resolved': {}, 'no_resueltas': [], 'matches': []}

        matches = self.extract_entities(message)
        resolved = {}
        no_resueltas = []

        for match in matches:
            entidad = match.get('entidad_nombre', 'no_clasificado')
            termino = match.get('termino', '')

            if not termino:
                continue

            if entidad == 'modelo':
                resolved['modelo'] = termino
                resolved['ultimo_modelo'] = termino
                logger.info(f"✅ Modelo resuelto: {termino}")
            elif entidad == 'producto':
                resolved['producto'] = termino
                logger.info(f"✅ Producto resuelto: {termino}")
            elif entidad == 'no_clasificado':
                if termino not in no_resueltas:
                    no_resueltas.append(termino)
                    logger.warning(f"⚠️ Sin clasificar: {termino}")

        if resolved:
            try:
                self.state_manager.update_state(contact_id, resolved)
            except Exception as e:
                logger.error(f"❌ Error actualizando estado en Redis: {e}")

        return {'resolved': resolved, 'no_resueltas': no_resueltas, 'matches': matches}