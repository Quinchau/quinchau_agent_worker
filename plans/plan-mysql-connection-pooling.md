 Plan: Conexión profesional a MySQL (pooling, resiliencia, consistencia)
## 1. Diagnóstico actual
Evidencia recolectada del código real (no hipótesis):
| Ubicación | Patrón actual | Problema |
|---|---|---|
| [`app/database.py:7`](../app/database.py:7) `get_db_connection()` | Abre una conexión `pymysql` **nueva** cada vez que se llama | Sin pool: cada llamada paga el costo completo de handshake TCP + auth de MySQL |
| [`app/catalog_cache.py:49-50`](../app/catalog_cache.py:49) `CatalogCache.__init__` | Guarda la conexión en `self.db` **una sola vez** | [`catalog_cache = CatalogCache()`](../app/catalog_cache.py:386) es un singleton global creado al importar el módulo → **una única conexión vive durante todo el proceso del worker** (horas/días) |
| [`app/intenciones/sin_clasificar.py:141-152`](../app/intenciones/sin_clasificar.py:141) y [`app/intenciones/informacion_general.py:161-172`](../app/intenciones/informacion_general.py:161) | Abren conexión con `try/finally: conn.close()` **por cada llamada** | Correcto en aislamiento, pero **inconsistente** con el patrón de `catalog_cache.py` — dos filosofías de manejo de conexión conviven en el mismo proyecto |
| [`app/jobs.py:41-44`](../app/jobs.py:41) `_get_db()` | Reusa `get_db_connection()` pero además importa `sqlalchemy.text` | Mezcla `pymysql` crudo con sintaxis de `sqlalchemy` sin usar un `Engine` real |
| [`requirements.txt`](../requirements.txt:4) | `sqlalchemy` está declarado | **Nunca se usa un `Engine`/pool** de SQLAlchemy en ningún archivo — dependencia fantasma |
### Riesgos concretos de la conexión singleton en `catalog_cache.py`
1. **`MySQL server has gone away`**: MySQL cierra conexiones inactivas tras `wait_timeout` (8h por defecto). El worker corre indefinidamente → la conexión de `catalog_cache.db` eventualmente queda muerta y **no hay reconexión automática**.
2. **No es thread-safe**: si en algún momento se corre más de un worker thread/proceso compartiendo el mismo objeto `CatalogCache`, `pymysql.Connection` no soporta uso concurrente.
3. **Sin healthcheck real de BD**: [`app/main.py:37-45`](../app/main.py:37) solo verifica Redis en `/health`, nunca MySQL.
4. **Sin retry/backoff** ante caídas transitorias de red hacia MySQL.
## 2. Objetivo del rediseño
- **Una sola forma de acceder a MySQL** en todo el proyecto (eliminar la dualidad conexión-larga vs conexión-por-llamada).
- **Pool de conexiones real** con reciclaje automático (`pool_recycle`) y verificación de salud antes de cada uso (`pool_pre_ping`).
- **Reconexión transparente** ante `MySQL server has gone away` sin intervención manual.
- **Aprovechar la dependencia ya declarada** (`sqlalchemy`) en vez de mantener `pymysql` crudo + `sqlalchemy` a medias.
- **Healthcheck real** de MySQL en `/health`.
- Cero cambios de comportamiento funcional — esto es una refactorización de infraestructura, no de lógica de negocio.
## 3. Opciones evaluadas
| Opción | Descripción | Veredicto |
|---|---|---|
| A. Seguir con `pymysql` crudo + abrir/cerrar por llamada en todos lados | Elimina la conexión larga, pero no resuelve falta de pool ni reduce latencia de handshake repetido | ❌ Insuficiente para "profesional" |
| B. `pymysql` + `DBUtils.PooledDB` | Pool ligero, sin dependencias nuevas más allá de `DBUtils` | Viable, pero agrega una dependencia nueva cuando ya existe una mejor opción sin usar |
| **C. SQLAlchemy `Engine` con `QueuePool`** | Usa la dependencia **ya presente** en `requirements.txt`; soporta `pool_pre_ping`, `pool_recycle`, `pool_size`, `max_overflow` de forma nativa y probada en producción en miles de proyectos | ✅ **Recomendada** |
**Decisión: Opción C.** Es la que menos dependencias nuevas introduce (ya está en `requirements.txt` sin usarse) y es el estándar de facto en Python para pooling de MySQL.
## 4. Diseño técnico propuesto
### 4.1 Nuevo `app/database.py`
```python
import os
import logging
from contextlib import contextmanager
from dotenv import load_dotenv
from sqlalchemy import create_engine
from sqlalchemy.engine import Row
from sqlalchemy.exc import OperationalError
load_dotenv()
logger = logging.getLogger(__name__)
DB_URL = (
    f"mysql+pymysql://{os.getenv('MYSQL_USER', 'root')}:"
    f"{os.getenv('MYSQL_PASSWORD', '')}@"
    f"{os.getenv('MYSQL_HOST', 'localhost')}:"
    f"{int(os.getenv('MYSQL_PORT', 3307))}/"
    f"{os.getenv('MYSQL_DB', 'quinchau')}?charset=utf8mb4"
)
engine = create_engine(
    DB_URL,
    pool_size=int(os.getenv("MYSQL_POOL_SIZE", 5)),
    max_overflow=int(os.getenv("MYSQL_POOL_MAX_OVERFLOW", 10)),
    pool_recycle=int(os.getenv("MYSQL_POOL_RECYCLE", 1800)),  # 30 min, < wait_timeout
    pool_pre_ping=True,   # valida la conexión antes de entregarla (evita "gone away")
    pool_timeout=int(os.getenv("MYSQL_POOL_TIMEOUT", 10)),
    future=True,
)
@contextmanager
def get_db_cursor(dict_cursor: bool = True):
    """
    Reemplazo directo del viejo get_db_connection() + conn.cursor().
    Uso:
        with get_db_cursor() as cursor:
            cursor.execute(query, params)
            rows = cursor.fetchall()
    Cierra y devuelve la conexión al pool automáticamente al salir del bloque.
    """
    conn = engine.raw_connection()  # conexión pymysql real, tomada del pool
    try:
        cursor = conn.cursor()  # ya viene configurado como DictCursor vía pymysql driver args
        yield cursor
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()  # NO cierra la conexión física: la devuelve al pool
def test_db_connection() -> bool:
    try:
        with engine.connect() as conn:
            conn.exec_driver_sql("SELECT 1")
        logger.info("✅ Conexión a MySQL (pool) verificada")
        return True
    except OperationalError as e:
        logger.error(f"❌ Error conectando a MySQL: {e}")
        return False
```
> Nota: `dict_cursor` se resuelve pasando `cursorclass=pymysql.cursors.DictCursor` como `connect_args` en `create_engine(..., connect_args={"cursorclass": DictCursor})`, para no cambiar el formato de fila (`dict`) que ya consume el resto del código (`row['nombre']`).
### 4.2 Migración de cada callsite
| Archivo | Cambio |
|---|---|
| [`app/catalog_cache.py`](../app/catalog_cache.py:49) | `__init__` deja de guardar `self.db = get_db_connection()`. Cada método `_load_*_from_db()` abre su propio `with get_db_cursor() as cursor:` puntual (igual patrón que ya usan `sin_clasificar.py`/`informacion_general.py`, ahora respaldado por pool). |
| [`app/intenciones/sin_clasificar.py`](../app/intenciones/sin_clasificar.py:141) | Cambiar `conn = get_db_connection()` + `try/finally: conn.close()` por `with get_db_cursor() as cursor:` (se simplifica, ya no hay que manejar cierre manual). |
| [`app/intenciones/informacion_general.py`](../app/intenciones/informacion_general.py:161) | Mismo cambio que el anterior. |
| [`app/jobs.py`](../app/jobs.py:41) `_get_db()` | Eliminar el helper; usar directamente `engine` de SQLAlchemy (ya se usa `text()` de SQLAlchemy aquí, así que este archivo pasa a ser 100% consistente con el resto). |
| [`app/main.py`](../app/main.py:37) `/health` | Agregar verificación de MySQL: `from .database import test_db_connection` y devolver `"mysql": "ok" if test_db_connection() else "error"`. |
| `index_products.py`, `indexador_unificado.py`, `reindex_multi_term.py` | Fuera del alcance del worker en runtime (son scripts de indexación ejecutados manualmente/cron) — **no migrar** en esta fase salvo que también corran de forma prolongada. Evaluar en una fase 2 si se detectan los mismos síntomas. |
## 5. Variables de entorno nuevas (con defaults seguros)
```
MYSQL_POOL_SIZE=5
MYSQL_POOL_MAX_OVERFLOW=10
MYSQL_POOL_RECYCLE=1800
MYSQL_POOL_TIMEOUT=10
```
`pool_recycle` debe ser **menor** al `wait_timeout` configurado en el servidor MySQL (verificar con `SHOW VARIABLES LIKE 'wait_timeout';` antes de fijar el valor final).
## 6. Plan de pruebas
1. **Test de arranque**: `test_db_connection()` debe pasar contra la BD real de staging.
2. **Test de reconexión simulada**: matar manualmente la conexión desde MySQL (`KILL <id>`) mientras el worker está corriendo, y confirmar que la siguiente query no falla (gracias a `pool_pre_ping`).
3. **Test de concurrencia**: lanzar N jobs en paralelo (varios workers o hilos) contra `catalog_cache.get_herramientas()` y `get_terminos_patterns()` y verificar que no hay errores de conexión compartida.
4. **Test de larga duración**: dejar el worker corriendo > `pool_recycle` segundos sin tráfico, y confirmar que la primera query después de ese período sigue funcionando (antes fallaría con "gone away").
5. **Regresión funcional**: correr el flujo completo de `process_ghl_message` con un mensaje real de prueba y confirmar que la respuesta es idéntica a la actual (mismo comportamiento, solo cambia la infraestructura de conexión).
## 7. Checklist de implementación
- [ ] Reescribir [`app/database.py`](../app/database.py) con `create_engine` + `get_db_cursor()` + `test_db_connection()`
- [ ] Migrar `CatalogCache.__init__` y sus métodos `_load_*_from_db()` en [`app/catalog_cache.py`](../app/catalog_cache.py) al nuevo `get_db_cursor()`
- [ ] Migrar `app/intenciones/sin_clasificar.py` (bloque `faq_interactions`)
- [ ] Migrar `app/intenciones/informacion_general.py` (bloque `faq_interactions`)
- [ ] Eliminar `_get_db()` de `app/jobs.py` y usar `engine` de SQLAlchemy directamente
- [ ] Agregar chequeo de MySQL en `/health` de `app/main.py`
- [ ] Agregar las 4 variables de entorno nuevas al `.env` / docker-compose / secretos de despliegue
- [ ] Verificar `wait_timeout` real del servidor MySQL de producción y ajustar `MYSQL_POOL_RECYCLE` en consecuencia
- [ ] Ejecutar el plan de pruebas (sección 6) en staging antes de desplegar a producción
- [ ] Monitorear logs por 24-48h post-deploy buscando cualquier `OperationalError` residual
## 8. Riesgos y mitigación
| Riesgo | Mitigación |
|---|---|
| `create_engine` con URL mal formada rompe el arranque del worker | Probar `test_db_connection()` en un script aislado antes de integrar a `catalog_cache.py` |
| Cambiar el tipo de fila retornado (SQLAlchemy `Row` vs `dict` de `pymysql.DictCursor`) rompe accesos tipo `row['campo']` en el resto del código | Usar `connect_args={"cursorclass": DictCursor}` para que `engine.raw_connection()` siga entregando cursores tipo `dict`, preservando 100% de compatibilidad con el código existente |
| Pool mal dimensionado (`pool_size` muy bajo) genera `TimeoutError` bajo carga | Empezar conservador (5 + 10 overflow) y ajustar según métricas reales de conexiones concurrentes en producción |
| Rollback necesario | Mantener el `app/database.py` actual respaldado (git permite revertir el commit puntual sin afectar el resto de cambios) |
## 9. Fuera de alcance de este plan
- Migrar los scripts de indexación (`index_products.py`, `indexador_unificado.py`, `reindex_multi_term.py`) — corren de forma independiente al worker y no comparten el problema de conexión de larga duración.
- Agregar tests automatizados (unitarios/integración) — se recomienda como plan separado, ya identificado como brecha en el análisis de arquitectura previo.
- Hardening de seguridad de la API (CORS, auth, rate limiting) — plan separado.
Approve
