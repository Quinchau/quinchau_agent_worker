# app/entity_resolver.py
import re
import logging
from datetime import datetime
from typing import Dict, Any, List, Optional
from .catalog_cache import CatalogCache
from .agent_state import AgentStateManager

logger = logging.getLogger(__name__)


class EntityResolver:
    def __init__(self):
        self.cache = CatalogCache()
        self.state_manager = AgentStateManager()
    
    def normalize_text(self, text: str) -> str:
        """
        Normaliza texto: lowercase, strip, elimina acentos.
        """
        if not text:
            return ""
        text = text.lower().strip()
        import unicodedata
        text = unicodedata.normalize('NFKD', text)
        text = ''.join([c for c in text if not unicodedata.combining(c)])
        return text

    # ============================================
    # NUEVO FLUJO: BÚSQUEDA SEMÁNTICA (SOLO UN MATCH)
    # ============================================

    def buscar_termino(self, texto: str, tipo_entidad: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """
        Busca el MEJOR match en el catálogo.
        
        RETORNA:
        - Dict con el mejor match (termino, entidad_nombre, id_entidad)
        - None si no encuentra nada
        
        NO retorna listas, NO retorna scores, NO retorna ambigüedad.
        Solo importa si existe o no existe el término.
        """
        if not texto:
            return None
        
        normalized = self.normalize_text(texto)
        all_patterns = self.cache.get_terminos_patterns()
        
        best_match = None
        best_priority = 0
        
        for item in all_patterns:
            # Filtrar por tipo de entidad si se especifica
            if tipo_entidad and item['entidad_nombre'] != tipo_entidad:
                continue
            
            # Verificar si hay coincidencia
            if self._text_matches(normalized, item['pattern']):
                # Calcular prioridad para elegir el mejor
                priority = self._calculate_priority(normalized, item['pattern'])
                
                if priority > best_priority:
                    best_priority = priority
                    best_match = {
                        'termino': item['termino'],
                        'entidad_nombre': item['entidad_nombre'],
                        'id_entidad': item['id_entidad'],
                        'pattern': item['pattern'],
                        'termino_id': item.get('termino_id')
                    }
        
        if best_match:
            logger.info(f"🔍 Búsqueda: '{texto}' → '{best_match['termino']}'")
            return best_match
        
        logger.info(f"🔍 Búsqueda: '{texto}' → Sin resultados")
        return None

    def buscar_producto(self, texto: str) -> Optional[Dict[str, Any]]:
        """Helper: busca solo productos."""
        return self.buscar_termino(texto, tipo_entidad='producto')

    def buscar_modelo(self, texto: str) -> Optional[Dict[str, Any]]:
        """Helper: busca solo modelos."""
        return self.buscar_termino(texto, tipo_entidad='modelo')

    def _text_matches(self, query: str, term: str) -> bool:
        """
        Verifica si query coincide con término del catálogo.
        
        SOLO matchea términos completos, NO subcadenas parciales.
        Ej: "bujia" NO debe matchear "bobina bujia"
        """
        if not query or not term:
            return False
        
        query = query.lower().strip()
        term = term.lower().strip()
        
        # Exact match (mejor caso)
        if query == term:
            return True
        
        query_words = query.split()
        term_words = term.split()
        
        # Query contiene el término como palabra completa
        # Ej: "bujia iridium" contiene "bujia"
        if len(query_words) > 1 and term in query_words:
            return True
        
        # Término contiene la query como palabra completa
        # Ej: "bujia" está en "bujia iridium"
        if len(term_words) > 1 and query in term_words:
            return True
        
        # Todas las palabras de query están en term
        # Ej: "freno delantero" y "freno_delantero"
        if query_words:
            if all(word in term_words for word in query_words):
                return True
        
        return False

    def _calculate_priority(self, query: str, term: str) -> int:
        """
        Calcula prioridad para elegir el mejor match.
        Prioridad más alta = mejor match.
        
        SOLO se usa internamente para elegir entre múltiples matches.
        """
        query = query.lower().strip()
        term = term.lower().strip()
        
        # Exact match = mayor prioridad
        if query == term:
            return 100
        
        # Query es más específica (contiene el término)
        # Ej: "bujia iridium" contiene "bujia"
        if query in term:
            return 80
        
        # Término contiene la query
        # Ej: "bujia" está en "bujia iridium"
        if term in query:
            return 70
        
        # Palabras comunes
        query_words = set(query.split())
        term_words = set(term.split())
        common = query_words.intersection(term_words)
        
        if common:
            # Todas las palabras de query están en term
            if len(common) == len(query_words):
                return 50 + len(common) * 10
            return 30 + len(common) * 10
        
        return 0

    def resolver_con_entidades_llm(self, 
                                        contact_id: str, 
                                        entidades_llm: Dict[str, Any]) -> Dict[str, Any]:
            """
            El worker resuelve entidades usando el catálogo como fuente de verdad.
            IMPONE el tipo real de cada entidad basado en el catálogo.
            """
            if not contact_id or not entidades_llm:
                return {
                    'resolved': {},
                    'product_found': False,
                    'model_found': False,
                    'estado': 'no_encontrado',
                    'entidades_no_resueltas': ['producto', 'modelo'],
                    'contact_id': contact_id
                }
            
            resolved = {}
            product_found = False
            model_found = False
            
            # ============================================
            # PROCESAR TODAS LAS ENTIDADES DEL LLM
            # EL WORKER IMPONE EL TIPO REAL DEL CATÁLOGO
            # ============================================
            
            entidades_a_procesar = []
            
            if entidades_llm.get('producto'):
                entidades_a_procesar.append(('producto', entidades_llm['producto']))
            
            if entidades_llm.get('modelo'):
                entidades_a_procesar.append(('modelo', entidades_llm['modelo']))
            
            # Procesar cada entidad con búsqueda GENERAL
            for tipo_llm, texto in entidades_a_procesar:
                if not texto:
                    continue
                    
                # 🔍 BÚSQUEDA GENERAL - El worker IMPONE lo que es
                match = self.buscar_termino(texto)  # Sin filtro de tipo
                
                if match:
                    tipo_real = match['entidad_nombre']  # El catálogo dice qué es
                    termino_encontrado = match['termino']
                    
                    # ============================================
                    # EL WORKER IMPONE EL TIPO REAL DEL CATÁLOGO
                    # ============================================
                    if tipo_real == 'producto':
                        # El catálogo dice que es PRODUCTO
                        resolved['producto'] = termino_encontrado
                        product_found = True
                        
                        # Si el LLM dijo modelo, lo corregimos
                        if tipo_llm == 'modelo':
                            logger.info(f"🚨 EL WORKER CORRIGE AL LLM: '{texto}' es PRODUCTO (LLM dijo modelo)")
                        else:
                            logger.info(f"✅ Producto confirmado: '{termino_encontrado}'")
                            
                    elif tipo_real == 'modelo':
                        # El catálogo dice que es MODELO
                        resolved['modelo'] = termino_encontrado
                        resolved['ultimo_modelo'] = termino_encontrado
                        model_found = True
                        
                        # Si el LLM dijo producto, lo corregimos
                        if tipo_llm == 'producto':
                            logger.info(f"🚨 EL WORKER CORRIGE AL LLM: '{texto}' es MODELO (LLM dijo producto)")
                        else:
                            logger.info(f"✅ Modelo confirmado: '{termino_encontrado}'")
                else:
                    logger.info(f"❌ Término no encontrado en catálogo: '{texto}' (LLM dijo {tipo_llm})")
            
            # ============================================
            # DETERMINAR ESTADO - EL WORKER SETEA EL STATE
            # ============================================
            
            if product_found and model_found:
                estado = 'resuelto'
            elif product_found or model_found:
                estado = 'parcial'
            else:
                estado = 'no_encontrado'
            
            entidades_no_resueltas = []
            if not product_found:
                entidades_no_resueltas.append('producto')
            if not model_found:
                entidades_no_resueltas.append('modelo')
            
            # ============================================
            # EL WORKER SETEA EL STATE - FUENTE DE VERDAD
            # ============================================
            updates = {}
            
            # ⭐ Producto: El worker decide
            if product_found:
                updates['producto'] = resolved.get('producto')
                updates['product_found'] = True
            else:
                # Si el LLM dijo producto pero no existe en catálogo, NO se setea
                updates['producto'] = None
                updates['product_found'] = False
            
            # ⭐ Modelo: El worker decide
            if model_found:
                updates['modelo'] = resolved.get('modelo')
                updates['ultimo_modelo'] = resolved.get('modelo')
                updates['model_found'] = True
            else:
                # Si el LLM dijo modelo pero no existe en catálogo, NO se setea
                updates['modelo'] = None
                updates['model_found'] = False
            
            updates['estado_resolucion'] = estado
            updates['entidades_no_resueltas'] = entidades_no_resueltas
            updates['updated_at'] = datetime.now().isoformat()
            
            logger.info(f"📦 EL WORKER SETEA EL STATE:")
            logger.info(f"   producto: {updates.get('producto')} (found={product_found})")
            logger.info(f"   modelo: {updates.get('modelo')} (found={model_found})")
            logger.info(f"   estado: {estado}")
            logger.info(f"   entidades_no_resueltas: {entidades_no_resueltas}")
            
            try:
                self.state_manager.update_state(contact_id, updates)
            except Exception as e:
                logger.error(f"❌ Error actualizando estado en Redis: {e}")
            
            return {
                'resolved': resolved,
                'product_found': product_found,
                'model_found': model_found,
                'estado': estado,
                'entidades_no_resueltas': entidades_no_resueltas,
                'contact_id': contact_id
            }

    # ============================================
    # MÉTODOS LEGACY (DEPRECADOS - PARA COMPATIBILIDAD)
    # ============================================

    def extract_entities(self, message: str) -> List[Dict[str, Any]]:
        """
        ⚠️ DEPRECADO: Extrae entidades del mensaje.
        
        Este método se mantiene por compatibilidad con el flujo antiguo.
        En el nuevo flujo, usar buscar_termino() en su lugar.
        """
        if not message:
            return []

        normalized = self.normalize_text(message)
        matches = []
        seen_terminos = set()
        covered_spans = []

        all_patterns = self.cache.get_terminos_patterns()
        all_patterns = sorted(all_patterns, key=lambda x: len(x['pattern']), reverse=True)

        for item in all_patterns:
            pattern = item['pattern']
            termino = item['termino']

            if termino in seen_terminos:
                continue

            pattern_regex = r'\b' + re.escape(pattern) + r'\b'
            for m in re.finditer(pattern_regex, normalized):
                start, end = m.span()

                if any(start < c_end and end > c_start for c_start, c_end in covered_spans):
                    continue

                seen_terminos.add(termino)
                covered_spans.append((start, end))
                matches.append({
                    'termino_id': item['termino_id'],
                    'termino': termino,
                    'id_entidad': item['id_entidad'],
                    'entidad_nombre': item['entidad_nombre'],
                    'alias': pattern
                })
                logger.debug(f"✅ Match: '{pattern}' → término '{termino}'")
                break

        logger.info(f"🔍 Encontrados {len(matches)} matches en mensaje")
        return matches

    def resolve_entities(self, message: str, contact_id: str) -> Dict[str, Any]:
        """
        ⚠️ DEPRECADO: Resuelve entidades y actualiza estado.
        
        Este método se mantiene por compatibilidad con el flujo antiguo.
        En el nuevo flujo, usar resolver_con_entidades_llm() en su lugar.
        """
        if not message or not contact_id:
            logger.warning("⚠️ message o contact_id vacío")
            return {'resolved': {}, 'no_resueltas': [], 'matches': []}

        matches = self.extract_entities(message)
        resolved = {}
        no_resueltas = []
        
        tiene_producto = False
        tiene_modelo = False

        for match in matches:
            entidad = match.get('entidad_nombre', 'no_clasificado')
            termino = match.get('termino', '')

            if not termino:
                continue

            if entidad == 'modelo':
                resolved['modelo'] = termino
                resolved['ultimo_modelo'] = termino
                tiene_modelo = True
                logger.info(f"✅ Modelo detectado: {termino}")
                
            elif entidad == 'producto':
                resolved['producto'] = termino
                tiene_producto = True
                logger.info(f"✅ Producto detectado: {termino}")
                
            elif entidad == 'no_clasificado':
                if termino not in no_resueltas:
                    no_resueltas.append(termino)
                    logger.warning(f"⚠️ Sin clasificar: {termino}")

        state = self.state_manager.get_state(contact_id) or {}
        updates = {}

        if tiene_producto:
            updates['producto'] = resolved['producto']
            logger.info(f"🔄 Producto SETEADO: '{resolved['producto']}'")

        if tiene_modelo:
            updates['modelo'] = resolved['modelo']
            updates['ultimo_modelo'] = resolved['ultimo_modelo']
            logger.info(f"🔄 Modelo SETEADO: '{resolved['modelo']}'")
        
        if no_resueltas:
            updates['entidades_no_resueltas'] = no_resueltas
            logger.info(f"📝 Términos no resueltos: {no_resueltas}")
        else:
            updates['entidades_no_resueltas'] = []
        
        updates['updated_at'] = datetime.now().isoformat()
        
        if updates:
            try:
                self.state_manager.update_state(contact_id, updates)
                logger.info(f"📦 Estado actualizado: {list(updates.keys())}")
                estado_nuevo = self.state_manager.get_state(contact_id)
                logger.info(f"📦 Nuevo estado: producto='{estado_nuevo.get('producto')}', modelo='{estado_nuevo.get('modelo')}'")
            except Exception as e:
                logger.error(f"❌ Error actualizando estado en Redis: {e}")
        
        return {
            'resolved': resolved,
            'no_resueltas': no_resueltas,
            'matches': matches,
            'tiene_producto': tiene_producto,
            'tiene_modelo': tiene_modelo,
            'contact_id': contact_id
        }