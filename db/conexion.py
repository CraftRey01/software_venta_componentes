import psycopg2
from queue import Empty, Full, Queue
from threading import Lock

from config import Config

_CATALOGO_POOL_SIZE = 64
_catalogo_pool = Queue(maxsize=_CATALOGO_POOL_SIZE)
_catalogo_pool_lock = Lock()
_catalogo_pool_size = 0


def obtener_conexion():
    try:
        # Cambia "tu_password" por la contraseña real de tu PostgreSQL
        conexion = psycopg2.connect(
            host="localhost",
            database=Config.DB_NAME,
            user="postgres",
            password="AND777",
            options="-c search_path=comercial,public"
        )
        return conexion
    except psycopg2.Error as e:
        print(f"Error de BD: {e}")
        return None


def obtener_conexion_catalogo():
    global _catalogo_pool_size

    while True:
        try:
            conexion = _catalogo_pool.get_nowait()
        except Empty:
            with _catalogo_pool_lock:
                crear_conexion = _catalogo_pool_size < _CATALOGO_POOL_SIZE
                if crear_conexion:
                    _catalogo_pool_size += 1

            if crear_conexion:
                conexion = obtener_conexion()
                if conexion is None:
                    with _catalogo_pool_lock:
                        _catalogo_pool_size -= 1
                    return None
            else:
                conexion = _catalogo_pool.get()

        if not conexion.closed:
            return conexion

        with _catalogo_pool_lock:
            _catalogo_pool_size -= 1


def devolver_conexion_catalogo(conexion):
    global _catalogo_pool_size

    if conexion is None:
        return
    if conexion.closed:
        with _catalogo_pool_lock:
            _catalogo_pool_size -= 1
        return

    try:
        conexion.rollback()
    except psycopg2.Error:
        conexion.close()
        with _catalogo_pool_lock:
            _catalogo_pool_size -= 1
        raise

    try:
        _catalogo_pool.put_nowait(conexion)
    except Full:
        conexion.close()
        with _catalogo_pool_lock:
            _catalogo_pool_size -= 1
        raise RuntimeError("El pool de conexiones del catálogo está lleno.")


# Función auxiliar para autoincrementar IDs (ya que tu SQL no usa SERIAL)
def obtener_siguiente_id(cursor, tabla, columna_id):
    cursor.execute(
        "SELECT pg_advisory_xact_lock(hashtext(%s))",
        (f"comercial.{tabla}",),
    )
    cursor.execute(
        f"SELECT COALESCE(MAX({columna_id}), 0) + 1 AS siguiente_id FROM comercial.{tabla}"
    )
    fila = cursor.fetchone()
    if isinstance(fila, dict):
        return fila["siguiente_id"]
    return fila[0]
