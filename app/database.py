import pymysql
from dotenv import load_dotenv
import os

load_dotenv()

def get_db_connection():
    \"\"\"Crea y retorna una conexión a MySQL\"\"\"
    return pymysql.connect(
        host=os.getenv('MYSQL_HOST', 'localhost'),
        port=int(os.getenv('MYSQL_PORT', 3307)),
        user=os.getenv('MYSQL_USER', 'root'),
        password=os.getenv('MYSQL_PASSWORD', ''),
        database=os.getenv('MYSQL_DB', 'quinchau'),
        cursorclass=pymysql.cursors.DictCursor,
        autocommit=False
    )

def test_db_connection():
    \"\"\"Prueba de conexión a la base de datos\"\"\"
    try:
        conn = get_db_connection()
        with conn.cursor() as cursor:
            cursor.execute(\"SELECT 1 as test\")
            result = cursor.fetchone()
        conn.close()
        print(\"✓ Conexión a MySQL exitosa\")
        return True
    except Exception as e:
        print(f\"✗ Error conectando a MySQL: {e}\")
        return False
