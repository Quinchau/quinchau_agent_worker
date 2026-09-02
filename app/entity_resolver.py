# app/entity_resolver.py
import re
import logging
from typing import Dict, Any, Optional, List
from .catalog_cache import catalog_cache
from .agent_state import AgentStateManager

logger = logging.getLogger(__name__)


class EntityResolver:
    """
    Utilidad de matching léxico contra el catálogo de términos/alias
    (`terminos_semanticos` + `terminos_alias`).

    ROL ACTUAL (post-clasificador unificado): este módulo YA NO participa
    en el flujo caliente de resolución de intención. Tanto 'modelo' como
    'producto' se resuelven ahora en una sola pasada por el LLM
    clasificador (ver `app/classifier.py`), que hace matching semántico
    contra el diccionario de términos y devuelve el término canónico
    directamente. `tasks.py` valida esa salida con exact-match
    determinístico (`normalize_text` + comparación contra el set de
    términos cacheado), sin volver a pasar por `buscar_modelo`/
    `buscar_productos`.

    Los métodos de matching aproximado (`buscar_modelo`, `buscar_productos`,
    `_text_matches`, `_calculate_priority`) se conservan como utilidades
    de soporte para la cola de revisión de términos no resueltos: cuando
    el clasificador devuelve un texto que no matchea exacto contra ningún
    término canónico, estas funciones se usan offline para sugerir
    "esto probablemente es alias de X" y acelerar la revisión manual.
    No bloquean ningún turno de conversación.

    `resolver_modelo()` y `resolver_productos_alias()` quedan como código
    sin consumidor en el flujo caliente (ver docstring de cada uno) —
    no reintroducir su llamada desde `tasks.py`/handlers por costumbre;
    esa responsabilidad es ahora exclusiva del clasificador.
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
        términos/alias mediante matching léxico aproximado (substring +
        límites de palabra + prioridad por especificidad).

        Uso actual: soporte offline para la cola de términos no resueltos
        (sugerir candidato de alias). Ya no es llamado desde el flujo
        de resolución de intención (eso lo hace el LLM clasificador).
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

    def buscar_productos(self, texto: str) -> List[Dict[str, Any]]:
        """
        Busca los matches de tipo 'producto' en `texto`, sin solapamientos:
        si un alias más largo cubre el mismo tramo de texto que uno más corto
        (ej. "la instalacion" contiene a "instalacion"), solo se conserva el
        más largo/específico.

        Uso actual: soporte offline para la cola de términos no resueltos.
        Ya no es llamado desde el flujo de resolución de intención.
        """
        if not texto:
            return []

        normalized = self.normalize_text(texto)
        all_patterns = self.cache.get_terminos_patterns()

        candidatos = []
        for item in all_patterns:
            if item['entidad_nombre'] != 'producto':
                continue
            if self._text_matches(normalized, item['pattern']):
                pattern_normalizado = self.normalize_text(item['pattern'])
                candidatos.append({
                    'termino': item['termino'],
                    'pattern': item['pattern'],
                    'pattern_normalizado': pattern_normalizado,
                    'priority': self._calculate_priority(normalized, item['pattern']),
                })

        # Más largo (y luego mayor prioridad) primero
        candidatos.sort(key=lambda m: (len(m['pattern_normalizado']), m['priority']), reverse=True)

        aceptados = []
        for cand in candidatos:
            # Si el pattern de un candidato ya aceptado contiene a este
            # (como substring de palabra completa), es un match redundante
            solapa = any(
                cand['pattern_normalizado'] != ok['pattern_normalizado']
                and re.search(r'\b' + re.escape(cand['pattern_normalizado']) + r'\b', ok['pattern_normalizado'])
                for ok in aceptados
            )
            if not solapa:
                aceptados.append(cand)

        return aceptados

    def resolver_productos_alias(self, texto: str) -> List[Dict[str, str]]:
        """
        [SIN CONSUMIDOR EN EL FLUJO CALIENTE — ver docstring de la clase]

        Antes usado como Gate 2.7 (resolución de alias de producto previa
        al LLM). Reemplazado por la resolución semántica del clasificador
        unificado (`app/classifier.py`) + validación exact-match en
        `tasks.py`. Se conserva por si conviene reusar esta forma de salida
        (`{'producto': ..., 'alias': ...}`) desde la cola de no-resueltos;
        no reintroducir su llamada desde `tasks.py`/handlers.
        """
        matches = self.buscar_productos(texto)
        resueltos = [{'producto': m['termino'], 'alias': m['pattern']} for m in matches]

        for r in resueltos:
            logger.info(f"✅ Alias de producto: '{r['alias']}' → '{r['producto']}'")

        return resueltos

    def _text_matches(self, query: str, term: str) -> bool:
        """
        Verifica si query coincide con término del catálogo.
        Matchea términos completos, y también frases multi-palabra del
        término/alias que aparezcan contiguas dentro del query (o viceversa),
        respetando límites de palabra — NO matchea subcadenas parciales
        dentro de una palabra (ej. "70" no matchea dentro de "170").
        """
        if not query or not term:
            return False

        query_normalized = self.normalize_text(query)
        term_normalized = self.normalize_text(term)

        if query_normalized == term_normalized:
            return True

        # Frase contigua con límites de palabra (cubre alias multi-palabra
        # embebidos en un mensaje más largo, ej. term="dr 650" dentro de
        # query="suichera dr 650")
        if re.search(r'\b' + re.escape(term_normalized) + r'\b', query_normalized):
            return True
        if re.search(r'\b' + re.escape(query_normalized) + r'\b', term_normalized):
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
        [SIN CONSUMIDOR EN EL FLUJO CALIENTE — ver docstring de la clase]

        Antes usado como Gate 2.5 (resolución de modelo previa al LLM).
        Reemplazado por la resolución semántica del clasificador unificado
        (`app/classifier.py`) + validación exact-match en `tasks.py`.
        Se conserva por si conviene reusar esta forma de salida
        (`{'modelo': ..., 'alias': ...}`) desde la cola de no-resueltos;
        no reintroducir su llamada desde `tasks.py`/handlers.
        """
        if not texto:
            return None

        match = self.buscar_modelo(texto)
        if not match:
            return None

        modelo = match['termino']
        alias = match['pattern']
        logger.info(f"✅ Modelo (matching aproximado, uso offline): '{modelo}' (alias: '{alias}')")
        return {'modelo': modelo, 'alias': alias}

# ============================================
# INSTANCIA GLOBAL PARA IMPORTACIÓN
# ============================================
entity_resolver = EntityResolver()