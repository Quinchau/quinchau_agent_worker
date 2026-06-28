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