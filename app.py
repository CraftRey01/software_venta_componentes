import os
import secrets
from flask import Flask, redirect, url_for
from waitress import serve
from modulos.auth.routes import auth_bp
from modulos.tienda.routes import tienda_bp
from modulos.ventas.routes import ventas_bp
from modulos.reportes.routes import reportes_bp
from modulos.servicio_tecnico.routes import servicio_tecnico_bp
from modulos.productos.routes import productos_bp

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY") or secrets.token_hex(32)
app.config["MAX_CONTENT_LENGTH"] = 6 * 1024 * 1024

# Registro de Módulos (Arquitectura Multicapa - Controladores)
app.register_blueprint(auth_bp)
app.register_blueprint(tienda_bp)
app.register_blueprint(ventas_bp)
app.register_blueprint(reportes_bp)
app.register_blueprint(servicio_tecnico_bp)
app.register_blueprint(productos_bp, url_prefix='/productos')

@app.route('/')
def inicio():
    return redirect(url_for('tienda.catalogo'))

if __name__ == '__main__':
    serve(
        app,
        host="127.0.0.1",
        port=5000,
        threads=int(os.environ.get("WAITRESS_THREADS", "64")),
        connection_limit=512,
    )
