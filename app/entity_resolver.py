# app/entity_resolver.py
import re
import logging
from datetime import datetime
from .catalog_cache import CatalogCache
from .agent_state import AgentStateManager

logger = logging.getLogger(__name__)

class EntityResolver:
    def __init__(self):
        self.cache = CatalogCache()
        self.state_manager = AgentStateManager()
    
    def normalize_text(self, text):
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
        covered_spans = []  # rangos (start, end) ya cubiertos por un match más específico

        all_patterns = self.cache.get_terminos_patterns()

        # 🔑 FIX: ordenar por longitud de patrón (desc) para que los alias
        # más específicos ("embrague arranque") se evalúen ANTES que los
        # genéricos ("arranque"). Así el término correcto reserva su
        # porción del texto antes de que un alias corto intente matchearla.
        all_patterns = sorted(all_patterns, key=lambda x: len(x['pattern']), reverse=True)

        for item in all_patterns:
            pattern = item['pattern']
            termino = item['termino']

            if termino in seen_terminos:
                continue

            pattern_regex = r'\b' + re.escape(pattern) + r'\b'
            for m in re.finditer(pattern_regex, normalized):
                start, end = m.span()

                # 🔑 FIX: si este rango de texto ya fue cubierto por un
                # match más específico (más largo), lo ignoramos. Esto
                # evita que "arranque" (alias de Motor Arranque) matchee
                # dentro de "embrague arranque" ya asignado a CREMALLERA.
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
                break  # con un match de este patrón alcanza

        logger.info(f"🔍 Encontrados {len(matches)} matches en mensaje")
        return matches

    def resolve_entities(self, message, contact_id):
        """
        Resuelve entidades y actualiza estado con reglas simples:
        
        📌 REGLA 1: Si el mensaje tiene PRODUCTO → SETEAR producto
        📌 REGLA 2: Si el mensaje NO tiene PRODUCTO → SETEAR None (null)
        📌 REGLA 3: Si el mensaje tiene MODELO → SETEAR modelo
        📌 REGLA 4: Si el mensaje NO tiene MODELO → DEJAR IGUAL (no tocar)
        
        Esto asegura que el estado siempre refleje la realidad del último mensaje.
        """
        if not message or not contact_id:
            logger.warning("⚠️ message o contact_id vacío")
            return {'resolved': {}, 'no_resueltas': [], 'matches': []}

        matches = self.extract_entities(message)
        resolved = {}
        no_resueltas = []
        
        # Flags para saber qué se detectó en este mensaje
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

        # ============================================
        # 🔄 ACTUALIZAR ESTADO - REGLAS SIMPLES
        # ============================================
        state = self.state_manager.get_state(contact_id) or {}
        updates = {}

        # 📌 REGLA 1: Si tiene producto → SETEAR
        if tiene_producto:
            updates['producto'] = resolved['producto']
            logger.info(f"🔄 Producto SETEADO: '{resolved['producto']}'")
        # 📌 REGLA 2: Si NO tiene producto → NO TOCAR (mantener el existente)

        # 📌 REGLA 3: Si tiene modelo → SETEAR
        if tiene_modelo:
            updates['modelo'] = resolved['modelo']
            updates['ultimo_modelo'] = resolved['ultimo_modelo']
            logger.info(f"🔄 Modelo SETEADO: '{resolved['modelo']}'")
        # 📌 REGLA 4: Si NO tiene modelo → DEJAR IGUAL (no hacer nada)
        
        # ✅ Guardar términos no clasificados (si hay)
        if no_resueltas:
            updates['entidades_no_resueltas'] = no_resueltas
            logger.info(f"📝 Términos no resueltos: {no_resueltas}")
        else:
            # Limpiar no_resueltas si no hay
            updates['entidades_no_resueltas'] = []
        
        # ✅ Actualizar timestamp
        updates['updated_at'] = datetime.now().isoformat()
        
        # ✅ Aplicar actualizaciones
        if updates:
            try:
                self.state_manager.update_state(contact_id, updates)
                logger.info(f"📦 Estado actualizado: {list(updates.keys())}")
                # Log del estado resultante
                estado_nuevo = self.state_manager.get_state(contact_id)
                logger.info(f"📦 Nuevo estado: producto='{estado_nuevo.get('producto')}', modelo='{estado_nuevo.get('modelo')}'")
            except Exception as e:
                logger.error(f"❌ Error actualizando estado en Redis: {e}")
        
        # ============================================
        # 📊 Retornar resultado con metadata
        # ============================================
        return {
            'resolved': resolved,
            'no_resueltas': no_resueltas,
            'matches': matches,
            'tiene_producto': tiene_producto,
            'tiene_modelo': tiene_modelo,
            'contact_id': contact_id
        }