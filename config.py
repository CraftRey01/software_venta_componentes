import os


class Config:
    # Datos de conexión a PostgreSQL — ajusta estos valores a tu entorno local
    DB_HOST = os.environ.get("DB_HOST", "localhost")
    DB_PORT = int(os.environ.get("DB_PORT", 5432))
    DB_USER = os.environ.get("DB_USER", "postgres")
    DB_PASSWORD = os.environ.get("DB_PASSWORD", "")
    DB_NAME = os.environ.get("DB_NAME", "sistema_comercial")

    # Clave usada por Flask para firmar la sesión (cámbiala en producción)
    SECRET_KEY = os.environ.get("SECRET_KEY", "cambia-esta-clave-en-produccion")

    # Punto de Reorden (ROP): parámetros generales usados por el módulo de reportes/productos
    TIEMPO_ENTREGA_DIAS = 5
    STOCK_SEGURIDAD_DEFECTO = 2
    DEMANDA_PERIODO_DIAS = 30
