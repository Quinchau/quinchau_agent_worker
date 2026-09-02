"""
RQ Worker — Dedicado exclusivamente al procesamiento de trabajos del dominio Realtor (ej. Cool Leads).

Arrancar localmente con:
    python app/worker_realtor.py

O en Docker con el comando definido en docker-compose (servicio quinchau-cool-leads-worker).
Escucha únicamente las colas de Realtor.
"""

import os
import sys
import logging

# ============================================
# CONFIGURACIÓN DE LOGGING
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
# AGREGAR RUTA DEL PROYECTO AL PYTHONPATH
# ============================================
# Esto permite que los imports "from app.xxx" funcionen correctamente
ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

from rq import Worker
from app.redis_queue import get_redis, QUEUE_REALTOR_COOL_LEADS, QUEUE_REALTOR_PENDING_REVIEWS

if __name__ == "__main__":
    redis_conn = get_redis()
    
    # Definición explícita de las colas que procesa este worker dedicado
    queues = [QUEUE_REALTOR_COOL_LEADS, QUEUE_REALTOR_PENDING_REVIEWS]

    logging.info("=" * 60)
    logging.info("🚀 Worker Realtor iniciando")
    logging.info(f"📂 ROOT_DIR: {ROOT_DIR}")
    logging.info(f"📬 Escuchando cola(s): {queues}")
    logging.info(f"🔧 DEBUG: {DEBUG}")
    logging.info("=" * 60)
    
    worker = Worker(queues, connection=redis_conn)
    
    try:
        worker.work()
    except KeyboardInterrupt:
        logging.info("🛑 Worker detenido manualmente (KeyboardInterrupt)")
    except Exception as e:
        logging.error(f"💥 Worker falló inesperadamente: {e}", exc_info=True)
        raise