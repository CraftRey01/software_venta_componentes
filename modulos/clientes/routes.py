from flask import Blueprint, render_template, request, redirect, url_for, flash
from db.conexion import ejecutar_consulta, ejecutar_cambio
from modulos.auth.routes import login_requerido

clientes_bp = Blueprint("clientes", __name__, template_folder="../../templates/clientes")


@clientes_bp.route("/")
@login_requerido
def listar():
    busqueda = request.args.get("q", "").strip()
    sql = "SELECT * FROM clientes"
    parametros = []
    if busqueda:
        sql += " WHERE nombres LIKE %s OR apellidos LIKE %s OR telefono LIKE %s"
        comodin = f"%{busqueda}%"
        parametros = [comodin, comodin, comodin]
    sql += " ORDER BY apellidos, nombres"

    clientes = ejecutar_consulta(sql, parametros)
    return render_template("clientes/listar.html", clientes=clientes, busqueda=busqueda)


@clientes_bp.route("/nuevo", methods=["GET", "POST"])
@login_requerido
def nuevo():
    if request.method == "POST":
        datos = _leer_formulario(request)
        ejecutar_cambio(
            """INSERT INTO clientes (nombres, apellidos, telefono, email, direccion)
               VALUES (%s,%s,%s,%s,%s)""",
            (datos["nombres"], datos["apellidos"], datos["telefono"], datos["email"], datos["direccion"]),
        )
        flash(f"Cliente \"{datos['nombres']} {datos['apellidos']}\" registrado.", "exito")
        return redirect(url_for("clientes.listar"))

    return render_template("clientes/formulario.html", cliente=None)


@clientes_bp.route("/<int:id_cliente>/editar", methods=["GET", "POST"])
@login_requerido
def editar(id_cliente):
    cliente = ejecutar_consulta(
        "SELECT * FROM clientes WHERE id_cliente = %s", (id_cliente,), obtener_uno=True
    )
    if not cliente:
        flash("Cliente no encontrado.", "error")
        return redirect(url_for("clientes.listar"))

    if request.method == "POST":
        datos = _leer_formulario(request)
        ejecutar_cambio(
            """UPDATE clientes SET nombres=%s, apellidos=%s, telefono=%s, email=%s, direccion=%s
               WHERE id_cliente=%s""",
            (datos["nombres"], datos["apellidos"], datos["telefono"], datos["email"],
             datos["direccion"], id_cliente),
        )
        flash("Cliente actualizado.", "exito")
        return redirect(url_for("clientes.listar"))

    return render_template("clientes/formulario.html", cliente=cliente)


@clientes_bp.route("/<int:id_cliente>/historial")
@login_requerido
def historial(id_cliente):
    cliente = ejecutar_consulta(
        "SELECT * FROM clientes WHERE id_cliente = %s", (id_cliente,), obtener_uno=True
    )
    ventas = ejecutar_consulta(
        "SELECT * FROM ventas WHERE id_cliente = %s ORDER BY fecha DESC", (id_cliente,)
    )
    servicios = ejecutar_consulta(
        "SELECT * FROM servicio_tecnico WHERE id_cliente = %s ORDER BY fecha_ingreso DESC",
        (id_cliente,),
    )
    return render_template(
        "clientes/historial.html", cliente=cliente, ventas=ventas, servicios=servicios
    )


def _leer_formulario(request):
    return {
        "nombres": request.form.get("nombres", "").strip(),
        "apellidos": request.form.get("apellidos", "").strip(),
        "telefono": request.form.get("telefono", "").strip(),
        "email": request.form.get("email", "").strip(),
        "direccion": request.form.get("direccion", "").strip(),
    }
