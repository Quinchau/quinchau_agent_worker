# app/agent_state.py
import json
import logging
from datetime import datetime
from .redis_queue import get_redis

logger = logging.getLogger(__name__)

class AgentStateManager:
    """Maneja el estado del agente en Redis"""
    
    def __init__(self):
        self.redis = get_redis()
    
    def get_state_key(self, contact_id):
        return f"agent:state:{contact_id}"
    
    def initialize_state(self, contact_id, contact_data):
        state_key = self.get_state_key(contact_id)
        
        if self.redis.get(state_key):
            return json.loads(self.redis.get(state_key))
        
        initial_state = {
            "modelo": None,
            "producto": None,
            "marca": None,
            "ultima_intencion": None,
            "ultimo_modelo": None,
            "productos_mencionados": [],
            "entidades_no_resueltas": [],
            "intentos_resolucion": 0,
            "status_conversacion": "active",
            "id_usuario": contact_id,
            "nombre_cliente": contact_data.get("first_name", ""),
            "telefono_cliente": contact_data.get("phone", ""),
            "email_cliente": contact_data.get("email", ""),
            # ============================================
            # NUEVOS CAMPOS PARA FLAGS DE RESOLUCIÓN
            # ============================================
            "product_found": False,
            "model_found": False,
            "estado_resolucion": "no_encontrado",
            "created_at": datetime.now().isoformat(),
            "updated_at": datetime.now().isoformat()
        }
        
        self.redis.setex(state_key, 86400, json.dumps(initial_state, default=str))
        logger.info(f"✅ Estado inicializado para {contact_id}")
        return initial_state
    
    def get_state(self, contact_id):
        state_key = self.get_state_key(contact_id)
        data = self.redis.get(state_key)
        if data:
            return json.loads(data)
        return None
    
    def update_state(self, contact_id, updates):
        """
        Actualiza el estado del usuario en Redis.
        
        AHORA: Permite agregar NUEVAS claves que no existían en el estado inicial.
        """
        state_key = self.get_state_key(contact_id)
        current = self.get_state(contact_id)
        if not current:
            return None
        
        # Actualizar todas las claves, incluyendo las nuevas
        for key, value in updates.items():
            current[key] = value  # ← AHORA permite claves nuevas
        
        current["updated_at"] = datetime.now().isoformat()
        self.redis.setex(state_key, 86400, json.dumps(current, default=str))
        return current
    
    def reset_resolution_flags(self, contact_id):
        """Resetea los flags de resolución después de procesar."""
        return self.update_state(contact_id, {
            "product_found": False,
            "model_found": False,
            "estado_resolucion": "no_encontrado",
            "entidades_no_resueltas": [],
            "intentos_resolucion": 0
        })
    
    def set_resolved(self, contact_id, producto, modelo):
        """Marca el estado como resuelto."""
        return self.update_state(contact_id, {
            "producto": producto,
            "modelo": modelo,
            "ultimo_modelo": modelo,
            "product_found": True,
            "model_found": True,
            "estado_resolucion": "resuelto",
            "entidades_no_resueltas": [],
            "intentos_resolucion": 0
        })