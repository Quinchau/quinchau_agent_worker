# app/entity_resolver.py
import logging
from typing import Dict, Any, Optional
from .catalog_cache import catalog_cache
from .agent_state import AgentStateManager

logger = logging.getLogger(__name__)


class EntityResolver:
    """
    Responsable exclusivo de resolver 'modelo' contra el catálogo de
    términos/alias, y de obtener el catálogo de productos de ese modelo.

    NOTA DE DISEÑO: 'producto' ya NO se resuelve aquí por texto libre.
    El producto se resuelve dentro del tool call del LLM, eligiendo sobre
    un enum de productos reales (ver tasks.py). Este resolver no necesita
    saber nada sobre producto.
    """

    def __init__(self):
        self.cache = catalog_cache
        self.state_manager = AgentStateManager()

    def normalize_text(self, text: str) -> str:
        """Normaliza texto: lowercase, strip, elimina acentos."""
        if not text:
            return ""
        text = text.lower().strip()
        import unicodedata
        text = unicodedata.normalize('NFKD', text)
        text = ''.join([c for c in text if not unicodedata.combining(c)])
        return text

    def buscar_modelo(self, texto: str) -> Optional[Dict[str, Any]]:
        """
        Busca el MEJOR match de tipo 'modelo' en el catálogo de
        términos/alias. Único método de matching usado en todo el pipeline
        (Gate 2.5 y segunda pasada post-LLM llaman a este mismo método).
        """
        if not texto:
            return None

        normalized = self.normalize_text(texto)
        all_patterns = self.cache.get_terminos_patterns()

        best_match = None
        best_priority = 0

        for item in all_patterns:
            if item['entidad_nombre'] != 'modelo':
                continue

            if self._text_matches(normalized, item['pattern']):
                priority = self._calculate_priority(normalized, item['pattern'])
                if priority > best_priority:
                    best_priority = priority
                    best_match = {
                        'termino': item['termino'],
                        'entidad_nombre': item['entidad_nombre'],
                        'id_entidad': item['id_entidad'],
                        'pattern': item['pattern'],
                        'termino_id': item.get('termino_id'),
                    }

        if best_match:
            logger.debug(f"🔍 Modelo encontrado: '{texto}' → '{best_match['termino']}'")

        return best_match

    def _text_matches(self, query: str, term: str) -> bool:
        """
        Verifica si query coincide con término del catálogo.
        SOLO matchea términos completos, NO subcadenas parciales.
        """
        if not query or not term:
            return False

        query_normalized = self.normalize_text(query)
        term_normalized = self.normalize_text(term)

        if query_normalized == term_normalized:
            return True

        query_words = query_normalized.split()
        term_words = term_normalized.split()

        if len(query_words) > 1 and term_normalized in query_words:
            return True

        if len(term_words) > 1 and query_normalized in term_words:
            return True

        if query_words and all(word in term_words for word in query_words):
            return True

        return False

    def _calculate_priority(self, query: str, term: str) -> int:
        """Calcula prioridad para elegir el mejor match entre candidatos."""
        query = self.normalize_text(query)
        term = self.normalize_text(term)

        if not query or not term:
            return 0

        if query == term:
            return 100
        if query in term:
            return 80
        if term in query:
            return 70

        query_words = set(query.split())
        term_words = set(term.split())
        common = query_words.intersection(term_words)

        if common:
            if len(common) == len(query_words):
                return 50 + len(common) * 10
            return 30 + len(common) * 10

        return 0

    def resolver_modelo(self, texto: str) -> Optional[Dict[str, str]]:
        """
        Busca el término de modelo en `texto`. Devuelve el modelo resuelto
        junto con el alias/pattern real que hizo match — necesario para
        poder normalizar ese alias en el mensaje antes de la llamada al LLM.
        """
        if not texto:
            return None

        match = self.buscar_modelo(texto)
        if not match:
            return None

        modelo = match['termino']
        alias = match['pattern']
        logger.info(f"✅ Gate 2.5: modelo '{modelo}' (alias: '{alias}')")
        return {'modelo': modelo, 'alias': alias}

# ============================================
# INSTANCIA GLOBAL PARA IMPORTACIÓN
# ============================================
entity_resolver = EntityResolver()