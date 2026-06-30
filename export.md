# Project Structure

```
app/
  __pycache__/
    __init__.cpython-312.pyc
    jobs.cpython-312.pyc
    main.cpython-312.pyc
    models.cpython-312.pyc
    redis_queue.cpython-312.pyc
    tasks.cpython-312.pyc
  __init__.py
  agent_state.py
  agent.py
  database.py
  entity_resolver.py
  intent_classifier.py
  jobs.py
  main.py
  models.py
  redis_queue.py
  tasks.py
  worker.py
skills/
.env
Dockerfile-agent
export.md
index_products.py
Quinchau_Agent_Indexacion.docx
reindex_multi_term.py
reindex_multitag.py
reindex_openrouter.py
reindex_with_models.py
reindex_with_tags.py
requirements.txt
```



# Selected Files Content

## app/entity_resolver.py

```py
# app/entity_resolver.py
import re
import logging
from .database import get_db_connection  # ✅ CORREGIDO: usa database.py
from .agent_state import AgentStateManager

logger = logging.getLogger(__name__)

class EntityResolver:
    def __init__(self):
        self.db = get_db_connection()
        self.state_manager = AgentStateManager()
    
    def normalize_text(self, text):
        """Normaliza el texto para búsqueda"""
        if not text:
            return ""
        return text.lower().strip()
    
    def extract_entities(self, message):
        """
        Extrae entidades del mensaje usando terminos_alias
        
        Returns:
            Lista de matches encontrados
        """
        if not message:
            return []
        
        normalized = self.normalize_text(message)
        matches = []
        
        query = """
        SELECT 
            ts.id as termino_id,
            ts.termino,
            ts.id_entidad,
            e.nombre as entidad_nombre,
            ta.alias
        FROM terminos_semanticos ts
        JOIN terminos_alias ta ON ts.id = ta.id_termino
        LEFT JOIN entidades e ON ts.id_entidad = e.id
        WHERE ts.activo = 1
        ORDER BY LENGTH(ta.alias) DESC
        """
        
        try:
            with self.db.cursor() as cursor:
                cursor.execute(query)
                results = cursor.fetchall()
        except Exception as e:
            logger.error(f"❌ Error en consulta SQL: {e}")
            return []
        
        # ✅ Usar un set para evitar duplicados por alias
        seen_aliases = set()
        
        for row in results:
            alias = row['alias'].lower()
            
            # ✅ Evitar procesar el mismo alias múltiples veces
            if alias in seen_aliases:
                continue
            seen_aliases.add(alias)
            
            # ✅ Buscar el alias como palabra completa
            pattern = r'\b' + re.escape(alias) + r'\b'
            if re.search(pattern, normalized):
                matches.append({
                    'termino_id': row['termino_id'],
                    'termino': row['termino'],
                    'id_entidad': row['id_entidad'],
                    'entidad_nombre': row['entidad_nombre'] or 'no_clasificado',
                    'alias': row['alias']
                })
        
        logger.info(f"🔍 Encontrados {len(matches)} matches en mensaje")
        return matches
    
    def resolve_entities(self, message, contact_id):
        """
        Resuelve entidades del mensaje y actualiza el estado en Redis
        
        Args:
            message: Mensaje del cliente
            contact_id: ID del contacto en GHL
        
        Returns:
            Dict con entidades resueltas y no resueltas
        """
        if not message or not contact_id:
            logger.warning("⚠️ message o contact_id vacío")
            return {
                'resolved': {},
                'no_resueltas': [],
                'matches': []
            }
        
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
                # ✅ Evitar duplicados en no_resueltas
                if termino not in no_resueltas:
                    no_resueltas.append(termino)
                    logger.warning(f"⚠️ Sin clasificar: {termino}")
        
        # ✅ Actualizar estado solo si hay cambios
        if resolved:
            try:
                self.state_manager.update_state(contact_id, resolved)
            except Exception as e:
                logger.error(f"❌ Error actualizando estado en Redis: {e}")
        
        return {
            'resolved': resolved,
            'no_resueltas': no_resueltas,
            'matches': matches
        }
```

## app/intent_classifier.py

```py
# app/intent_classifier.py
import logging
from .database import get_db_connection  # ✅ CORREGIDO: usa database.py

logger = logging.getLogger(__name__)

class IntentClassifier:
    def __init__(self):
        self.db = get_db_connection()
    
    def get_entidades_bloqueantes(self, intencion):
        """
        Obtiene las entidades bloqueantes para una intención
        
        Args:
            intencion: Nombre de la intención (ej. 'intencion_compra_repuestos')
        
        Returns:
            Lista de nombres de entidades bloqueantes
        """
        # ✅ Validar que intencion no esté vacía
        if not intencion:
            logger.warning("⚠️ intencion vacía en get_entidades_bloqueantes")
            return []
        
        query = """
        SELECT 
            e.nombre as entidad_nombre
        FROM intenciones i
        JOIN intencion_entidad ie ON i.id = ie.id_intencion
        JOIN entidades e ON ie.id_entidad = e.id
        WHERE i.nombre = %s AND ie.bloqueante = 1
        ORDER BY ie.orden_prioridad
        """
        
        try:
            with self.db.cursor() as cursor:
                cursor.execute(query, [intencion])
                resultados = cursor.fetchall()
                return [row['entidad_nombre'] for row in resultados]
        except Exception as e:
            logger.error(f"❌ Error consultando entidades bloqueantes para '{intencion}': {e}")
            return []
    
    def validate_entities(self, intencion, state):
        """
        Valida si todas las entidades bloqueantes están resueltas en el estado
        
        Args:
            intencion: Nombre de la intención
            state: Diccionario con el estado actual del usuario
        
        Returns:
            Lista de entidades faltantes (vacío si todo está resuelto)
        """
        # ✅ Validar parámetros
        if not intencion:
            logger.warning("⚠️ intencion vacía en validate_entities")
            return []
        
        if not state or not isinstance(state, dict):
            logger.warning("⚠️ state inválido en validate_entities")
            return []
        
        # ✅ Obtener entidades bloqueantes
        bloqueantes = self.get_entidades_bloqueantes(intencion)
        
        if not bloqueantes:
            logger.info(f"ℹ️ No hay entidades bloqueantes para '{intencion}'")
            return []
        
        faltantes = []
        
        for entidad in bloqueantes:
            # ✅ Verificar que la entidad existe en state y tiene valor
            valor = state.get(entidad)
            if valor is None or valor == "":
                faltantes.append(entidad)
                logger.info(f"⚠️ Entidad faltante: {entidad}")
        
        if faltantes:
            logger.info(f"⚠️ Faltan {len(faltantes)} entidades para '{intencion}': {faltantes}")
        else:
            logger.info(f"✅ Todas las entidades resueltas para '{intencion}'")
        
        return faltantes
```

## app/tasks.py

```py
"""
Tasks dispatcher.

En producción los jobs se encolan en Redis para que el worker los ejecute.
En modo SYNC_MODE=true ejecuta directo (útil para tests o ambientes sin Redis).
"""

import os
import logging
import httpx
from datetime import datetime
from typing import Dict, Any
from openai import OpenAI

from .redis_queue import get_queue, QUEUE_HIGH, QUEUE_AI, get_redis
from .jobs import job_classify_user_preference, job_general_chat
from .agent_state import AgentStateManager
from .entity_resolver import EntityResolver
from .intent_classifier import IntentClassifier

# ============================================
# CONFIGURACIÓN
# ============================================

logger = logging.getLogger(__name__)
SYNC_MODE = os.getenv("SYNC_MODE", "false").lower() == "true"
DEBUG = os.getenv("DEBUG", "false").lower() == "true"

# ============================================
# TAREAS PÚBLICAS (FastAPI)
# ============================================

async def classify_user_preference_task(data: Dict[str, Any]) -> Dict[str, Any]:
    """Encola clasificación en Redis (cola high) o ejecuta directo en modo sync"""
    if SYNC_MODE:
        return job_classify_user_preference(data)

    job = get_queue(QUEUE_HIGH).enqueue(
        job_classify_user_preference,
        data,
        job_timeout=60,
        result_ttl=3600,
    )
    return {
        "success": True,
        "status": "queued",
        "job_id": job.id,
        "category": None,
        "message": f"Job encolado — resultado disponible vía /job/{job.id}",
    }


async def general_chat_task(data: Dict[str, Any]) -> Dict[str, Any]:
    """Chat: siempre síncrono (respuesta inmediata esperada)"""
    from .agent import agent
    
    response = agent.general_chat(
        data.get("message", ""),
        data.get("chat_history", []),
    )
    return {"status": "success", "task_type": "chat", "response": response}


# ============================================
# TAREAS PARA GHL
# ============================================

def send_message_to_ghl(contact_id: str, message: str, channel: str = "WhatsApp") -> Dict[str, Any]:
    """
    Envía la respuesta generada por el LLM de vuelta al contacto en GHL
    usando la Conversations API (Send a new message).
    """
    token = os.getenv("GHL_PRIVATE_TOKEN")
    if not token:
        raise ValueError("GHL_PRIVATE_TOKEN no configurada")

    url = "https://services.leadconnectorhq.com/conversations/messages"
    headers = {
        "Authorization": f"Bearer {token}",
        "Version": "2021-04-15",
        "Content-Type": "application/json",
    }
    payload = {
        "type": channel,
        "contactId": contact_id,
        "message": message,
    }

    resp = httpx.post(url, json=payload, headers=headers, timeout=10)
    resp.raise_for_status()
    return resp.json()


def process_ghl_message(task_data: Dict[str, Any]) -> Dict[str, Any]:
    """Procesa el mensaje de GHL con LLM + ENTIDADES ESTRUCTURADAS"""
    
    try:
        # ============================================
        # 1. DATOS DEL USUARIO
        # ============================================
        message = task_data.get('message', '')
        first_name = task_data.get('first_name', 'Cliente')
        last_name = task_data.get('last_name', '')
        contact_id = task_data.get('contact_id')
        channel = task_data.get('channel', 'WhatsApp')

        if not contact_id:
            raise ValueError("contact_id no presente en task_data")

        logger.info("=" * 60)
        logger.info(f"📥 Mensaje: {message}")
        logger.info(f"👤 Usuario: {first_name} {last_name} ({contact_id})")
        logger.info("=" * 60)

        # ============================================
        # 2. ESTADO EN REDIS
        # ============================================
        state_manager = AgentStateManager()
        
        contact_data = {
            "first_name": first_name,
            "last_name": last_name,
            "phone": task_data.get('phone', ''),
            "email": task_data.get('email', '')
        }
        
        state = state_manager.get_state(contact_id)
        if not state:
            logger.info(f"🆕 Nuevo contacto, inicializando...")
            state = state_manager.initialize_state(contact_id, contact_data)
        else:
            logger.info(f"📦 Estado recuperado de Redis")

        # ============================================
        # 3. RESOLVER ENTIDADES DEL MENSAJE
        # ============================================
        resolver = EntityResolver()
        resolution = resolver.resolve_entities(message, contact_id)

        # ✅ ACTUALIZAR ESTADO CON ENTIDADES RESUELTAS
        if resolution['resolved']:
            state_manager.update_state(contact_id, resolution['resolved'])
            # Recargar estado actualizado
            state = state_manager.get_state(contact_id)

        if resolution['resolved']:
            logger.info(f"🔍 Entidades resueltas: {resolution['resolved']}")
        else:
            logger.info(f"🔍 Entidades resueltas: Ninguna")

        # ============================================
        # 4. CLASIFICAR INTENCIÓN
        # ============================================
        intent_classifier = IntentClassifier()
        
        intent_prompt = f"""
        Clasifica la intención del siguiente mensaje de un cliente de una tienda de motos.
        
        Mensaje: "{message}"
        
        Intenciones posibles:
        - intencion_compra_repuestos: El cliente quiere comprar un repuesto o accesorio
        - consulta_disponibilidad: El cliente pregunta si hay stock de un producto
        - consulta_precio: El cliente pregunta el precio de un producto
        - consulta_ubicacion_horario: El cliente pregunta por ubicación u horarios
        - reporte_incidente: El cliente reporta un incidente o daño
        - agendar_cita: El cliente quiere agendar una cita de servicio
        - orden_sin_despacho: El cliente consulta por un pedido no entregado
        - sin_clasificar: No se puede clasificar claramente
        
        Responde SOLO con el nombre de la intención, sin explicación.
        """
        
        client = OpenAI(
            base_url="https://openrouter.ai/api/v1",
            api_key=os.getenv("OPENROUTER_API_KEY"),
        )
        
        intent_response = client.chat.completions.create(
            model="openai/gpt-4o-mini",
            messages=[{"role": "user", "content": intent_prompt}],
            temperature=0.1,
            max_tokens=20,
        )
        
        intencion = intent_response.choices[0].message.content.strip()
        logger.info(f"🎯 Intención: {intencion}")

        # ============================================
        # 5. VALIDAR ENTIDADES REQUERIDAS
        # ============================================
        faltantes = intent_classifier.validate_entities(intencion, state)
        
        if faltantes:
            logger.info(f"⚠️ Faltan entidades: {faltantes}")
            
            # ✅ Construir pregunta específica
            producto = state.get('producto')
            if producto and 'producto' not in faltantes:
                # Si el producto ya está resuelto, preguntar solo por el modelo
                respuesta_pregunta = f"¿Para qué modelo de moto necesitas {producto}?"
            else:
                # Si falta el producto también, preguntar por ambos
                respuesta_pregunta = "¿Qué producto o repuesto necesitas y para qué modelo de moto?"
            
            # Guardar estado
            state_manager.update_state(contact_id, {
                "ultima_intencion": intencion,
                "entidades_no_resueltas": faltantes
            })
            
            send_message_to_ghl(contact_id, respuesta_pregunta, channel)
            logger.info(f"📤 Pregunta enviada: {respuesta_pregunta}")
            
            return {
                "success": True,
                "response": respuesta_pregunta,
                "contact_id": contact_id,
                "intencion": intencion,
                "entidades_faltantes": faltantes,
                "processed_at": datetime.now().isoformat()
            }

        # ============================================
        # 6. PROMPT DEL LLM
        # ============================================
        entidades_texto = ""
        if state.get('modelo'):
            entidades_texto += f"- Modelo de moto: {state['modelo']}\n"
        if state.get('producto'):
            entidades_texto += f"- Producto: {state['producto']}\n"
        if state.get('ultimo_modelo'):
            entidades_texto += f"- Último modelo mencionado: {state['ultimo_modelo']}\n"
        
        turnos = state.get('ultimos_turnos', [])[-3:]
        historial_texto = ""
        if turnos:
            historial_texto = "Historial reciente:\n"
            for t in turnos:
                historial_texto += f"Cliente: {t['cliente']}\n"
                historial_texto += f"Asistente: {t['asistente']}\n\n"

        system_prompt = f"""
        Eres un asistente de ventas de Quinchau Motos, una tienda de motos.
        Tu nombre es Quinchau Assistant.
        El cliente se llama {first_name} {last_name}.

        INFORMACIÓN CONOCIDA DEL CLIENTE:
        {entidades_texto if entidades_texto else "Aún no tenemos información específica del cliente."}

        {historial_texto}

        Reglas:
        - Usa la información conocida del cliente para ser más preciso.
        - Responde de manera amable, profesional y concisa.
        - NO adivines productos o modelos que no estén en la información conocida.
        """

        user_prompt = f"""
        Mensaje del cliente: {message}

        Intención detectada: {intencion}
        """

        # ============================================
        # 7. MOSTRAR PROMPT (SIMPLIFICADO)
        # ============================================
        logger.info("=" * 60)
        logger.info("📝 PROMPT DEL LLM:")
        logger.info("-" * 40)
        logger.info(f"🧠 System: {system_prompt.strip()[:200]}...")
        logger.info(f"👤 User: {message}")
        logger.info("=" * 60)

        # ============================================
        # 8. GENERAR RESPUESTA
        # ============================================
        logger.info("🔄 Generando respuesta...")
        
        response = client.chat.completions.create(
            model="openai/gpt-4o-mini",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}
            ],
            temperature=0.3,
            max_tokens=300,
        )

        llm_response = response.choices[0].message.content

        # ============================================
        # 9. MOSTRAR RESPUESTA
        # ============================================
        logger.info("=" * 60)
        logger.info("📨 RESPUESTA DEL LLM:")
        logger.info("-" * 40)
        logger.info(llm_response)
        logger.info("=" * 60)

        # ============================================
        # 10. ACTUALIZAR ESTADO
        # ============================================
        state_manager.add_turno(contact_id, message, llm_response)
        
        state_manager.update_state(contact_id, {
            "ultima_intencion": intencion,
            "ultimo_modelo": state.get('modelo')
        })

        # ============================================
        # 11. ENVIAR A GHL
        # ============================================
        send_message_to_ghl(contact_id, llm_response, channel)
        logger.info(f"📤 Respuesta enviada a GHL")

        # ============================================
        # 12. MOSTRAR ESTADO EN REDIS
        # ============================================
        logger.info("=" * 60)
        logger.info("📊 ESTADO EN REDIS (contacto)")
        logger.info("-" * 40)
        estado_actual = state_manager.get_state(contact_id)
        if estado_actual:
            logger.info(f"👤 Nombre: {estado_actual.get('nombre_cliente', 'N/A')}")
            logger.info(f"🏍️ Modelo: {estado_actual.get('modelo', 'N/A')}")
            logger.info(f"📦 Producto: {estado_actual.get('producto', 'N/A')}")
            logger.info(f"🎯 Intención: {estado_actual.get('ultima_intencion', 'N/A')}")
            logger.info(f"📝 Turnos: {estado_actual.get('turno_actual', 0)}")
        logger.info("=" * 60)

        logger.info("✅ Worker completado")
        
        return {
            "success": True,
            "response": llm_response,
            "contact_id": contact_id,
            "intencion": intencion,
            "entidades_resueltas": resolution['resolved'],
            "entidades_no_resueltas": resolution['no_resueltas'],
            "processed_at": datetime.now().isoformat()
        }

    except Exception as e:
        logger.error(f"❌ Error: {str(e)}")
        import traceback
        logger.error(traceback.format_exc())
        raise


def enqueue_ghl_message(task_data: Dict[str, Any]) -> Dict[str, Any]:
    """
    Encola un mensaje de GHL en la cola QUEUE_AI
    Usado por quinchau_agent (FastAPI)
    """
    queue = get_queue(QUEUE_AI)
    
    job = queue.enqueue(
        process_ghl_message,
        task_data,
        job_timeout=300,
        result_ttl=86400,
        failure_ttl=86400,
    )
    
    return {
        "success": True,
        "status": "queued",
        "job_id": job.id,
        "message": f"Job encolado en cola AI",
    }


# ============================================
# DICCIONARIO DE TAREAS
# ============================================

TASKS = {
    "classify_user_preference": classify_user_preference_task,
    "chat": general_chat_task,
    "process_ghl_message": process_ghl_message,
    "enqueue_ghl_message": enqueue_ghl_message,
}
```

## app/worker.py

```py
"""
RQ Worker — consume jobs de Redis y los ejecuta.

Arrancar con:
    python worker.py

O en Docker con el comando definido en docker-compose (ver servicio quinchau-agent-worker).
Escucha las colas: high, ai_tasks, default (en ese orden de prioridad).
"""

import os
import sys
import logging

# ============================================
# ✅ CONFIGURACIÓN DE LOGGING (ANTES DE TODO)
# ============================================
DEBUG = os.getenv("DEBUG", "false").lower() == "true"

logging.basicConfig(
    level=logging.DEBUG if DEBUG else logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    datefmt="%H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout)
    ]
)

# ============================================
# ✅ AGREGAR RUTA DEL PROYECTO AL PYTHONPATH
# ============================================
ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

from rq import Worker
from app.redis_queue import get_redis, QUEUE_HIGH, QUEUE_AI, QUEUE_DEFAULT

if __name__ == "__main__":
    redis_conn = get_redis()
    queues = [QUEUE_HIGH, QUEUE_AI, QUEUE_DEFAULT]

    # ✅ Usar logging en lugar de print
    logging.info(f"🚀 Worker iniciando — colas: {queues}")
    logging.info(f"📂 ROOT_DIR: {ROOT_DIR}")
    logging.info(f"🔧 DEBUG: {DEBUG}")
    
    worker = Worker(queues, connection=redis_conn)
    worker.work(with_scheduler=True)
```

