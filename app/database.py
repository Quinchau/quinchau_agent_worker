import os
import logging
from contextlib import contextmanager
from dotenv import load_dotenv
from sqlalchemy import create_engine
from sqlalchemy.exc import OperationalError
import pymysql

load_dotenv()
logger = logging.getLogger(__name__)

# Construcción de URL de conexión a la base de datos
DB_URL = (
    f"mysql+pymysql://{os.getenv('MYSQL_USER', 'root')}:"
    f"{os.getenv('MYSQL_PASSWORD', '')}@"
    f"{os.getenv('MYSQL_HOST', 'localhost')}:"
    f"{int(os.getenv('MYSQL_PORT', 3307))}/"
    f"{os.getenv('MYSQL_DB', 'quinchau')}?charset=utf8mb4"
)

# Creación del Motor con Connection Pooling
engine = create_engine(
    DB_URL,
    pool_size=int(os.getenv("MYSQL_POOL_SIZE", 5)),
    max_overflow=int(os.getenv("MYSQL_POOL_MAX_OVERFLOW", 10)),
    pool_recycle=int(os.getenv("MYSQL_POOL_RECYCLE", 1800)),
    pool_pre_ping=True,
    pool_timeout=int(os.getenv("MYSQL_POOL_TIMEOUT", 10)),
    future=True,
)


@contextmanager
def get_db_cursor():
    """
    Administrador de contexto que extrae una conexión nativa de PyMySQL,
    crea un cursor de tipo diccionario para la aplicación sin romper
    las consultas internas de SQLAlchemy.
    """
    conn = engine.raw_connection()
    try:
        # Forzamos explícitamente el DictCursor solo para la sesión de trabajo
        cursor = conn.cursor(pymysql.cursors.DictCursor)
        try:
            yield cursor
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            cursor.close()
    finally:
        conn.close()


def test_db_connection() -> bool:
    """Valida la disponibilidad del pool de conexiones hacia la base de datos."""
    try:
        with engine.connect() as conn:
            conn.exec_driver_sql("SELECT 1")
        logger.info("✅ Conexión a MySQL (pool) verificada")
        return True
    except OperationalError as e:
        logger.error(f"❌ Error conectando a MySQL: {e}")
        return False