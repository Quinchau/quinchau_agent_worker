import redis
from rq import Queue
from dotenv import load_dotenv
import os

load_dotenv()

_redis_conn = None


def get_redis():
    """Conexión Redis singleton"""
    global _redis_conn
    if _redis_conn is None:
        password = os.getenv("REDIS_PASSWORD") or None
        _redis_conn = redis.Redis(
            host=os.getenv("REDIS_HOST", "localhost"),
            port=int(os.getenv("REDIS_PORT", 6379)),
            db=int(os.getenv("REDIS_DB", 0)),
            password=password,
            decode_responses=False,  # RQ necesita bytes
        )
    return _redis_conn


def get_queue(name: str = "default") -> Queue:
    return Queue(name, connection=get_redis())


QUEUE_DEFAULT  = "default"
QUEUE_AI       = "ai_tasks"
QUEUE_HIGH     = "high"