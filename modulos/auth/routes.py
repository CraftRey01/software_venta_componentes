from datetime import datetime, timedelta
from functools import wraps
import re

from flask import Blueprint, current_app, flash, redirect, render_template, request, session, url_for
import psycopg2
from psycopg2.extras import RealDictCursor
from werkzeug.security import check_password_hash, generate_password_hash

from db.conexion import obtener_conexion, obtener_siguiente_id

auth_bp = Blueprint("auth", __name__)
intentos_login = {}


def rol_requerido(*roles_permitidos):
    def decorador(vista):
        @wraps(vista)
        def envoltura(*args, **kwargs):
            if not session.get("usuario"):
                flash("Inicia sesión para acceder a esta sección.", "advertencia")
                return redirect(url_for("auth.login"))
            if session.get("rol") not in roles_permitidos:
                flash("Tu cuenta no tiene permiso para esa sección.", "error")
                return redirect(url_for("inicio"))
            return vista(*args, **kwargs)
        return envoltura
    return decorador


empleado_requerido = rol_requerido("VENDEDOR", "ADMINISTRADOR")
administrador_requerido = rol_requerido("ADMINISTRADOR")
cliente_requerido = rol_requerido("CLIENTE")


@auth_bp.route("/login", methods=["GET", "POST"])
def login():
    ip = request.remote_addr or "desconocida"
    ahora = datetime.now()
    estado = intentos_login.setdefault(ip, {"intentos": 0, "bloqueado_hasta": None})

    if estado["bloqueado_hasta"] and ahora < estado["bloqueado_hasta"]:
        segundos_restantes = int((estado["bloqueado_hasta"] - ahora).total_seconds())
        minutos = max(1, (segundos_restantes + 59) // 60)
        flash(f"Acceso bloqueado por seguridad. Intenta nuevamente en {minutos} min.", "error")
        return render_template("auth/login.html"), 429

    if request.method == "POST":
        usuario = request.form.get("usuario", "").strip()
        password = request.form.get("password", "")
        conexion = obtener_conexion()
        if conexion is None:
            flash("No se pudo conectar con la base de datos. Intenta más tarde.", "error")
            return render_template("auth/login.html"), 503

        try:
            with conexion.cursor(cursor_factory=RealDictCursor) as cursor:
                cursor.execute(
                    """SELECT id_usuario, usuario, password_hash, rol, id_cliente, id_vendedor
                       FROM comercial.usuario_sistema
                       WHERE usuario = %s AND activo = TRUE""",
                    (usuario,),
                )
                cuenta = cursor.fetchone()
        finally:
            conexion.close()

        if cuenta and check_password_hash(cuenta["password_hash"], password):
            estado["intentos"] = 0
            estado["bloqueado_hasta"] = None
            session.clear()
            session["usuario"] = cuenta["usuario"]
            session["rol"] = cuenta["rol"]
            session["id_usuario"] = cuenta["id_usuario"]
            if cuenta["id_cliente"] is not None:
                session["id_cliente"] = cuenta["id_cliente"]
            if cuenta["id_vendedor"] is not None:
                session["id_vendedor"] = cuenta["id_vendedor"]
            flash(f"Bienvenido, {cuenta['usuario']}.", "exito")
            if cuenta["rol"] == "VENDEDOR":
                return redirect(url_for("ventas.punto_venta"))
            if cuenta["rol"] == "ADMINISTRADOR":
                return redirect(url_for("reportes.ingresos"))
            if cuenta["rol"] == "CLIENTE":
                return redirect(url_for("tienda.catalogo"))
            session.clear()
            flash("El rol de esta cuenta no está configurado.", "error")
            return render_template("auth/login.html"), 403

        estado["intentos"] += 1
        if estado["intentos"] >= 3:
            estado["bloqueado_hasta"] = ahora + timedelta(minutes=5)
            estado["intentos"] = 0
            flash("Demasiados intentos fallidos. Acceso bloqueado por 5 minutos.", "error")
        else:
            flash("Usuario o contraseña incorrectos.", "error")

    return render_template("auth/login.html")


@auth_bp.route("/registro", methods=["GET", "POST"])
def registro():
    if session.get("usuario"):
        return redirect(url_for("tienda.catalogo"))

    datos = {
        "nombre": request.form.get("nombre", "").strip(),
        "telefono": request.form.get("telefono", "").strip(),
        "email": request.form.get("email", "").strip().lower(),
        "usuario": request.form.get("usuario", "").strip(),
        "direccion": request.form.get("direccion", "").strip(),
    }

    if request.method == "POST":
        password = request.form.get("password", "")
        confirmacion = request.form.get("confirmacion", "")
        errores = []

        if not datos["nombre"] or len(datos["nombre"]) > 150:
            errores.append("Ingresa un nombre de hasta 150 caracteres.")
        if len(datos["telefono"]) > 30:
            errores.append("El teléfono no puede superar 30 caracteres.")
        if len(datos["direccion"]) > 255:
            errores.append("La dirección no puede superar 255 caracteres.")
        if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", datos["email"]) or len(datos["email"]) > 254:
            errores.append("Ingresa un correo electrónico válido.")
        if not datos["usuario"] or len(datos["usuario"]) > 60:
            errores.append("El usuario debe tener entre 1 y 60 caracteres.")
        if len(password) < 8:
            errores.append("La contraseña debe tener al menos 8 caracteres.")
        if password != confirmacion:
            errores.append("Las contraseñas no coinciden.")

        if errores:
            for error in errores:
                flash(error, "error")
            return render_template("auth/registro.html", datos=datos), 400

        conexion = obtener_conexion()
        if conexion is None:
            flash("No se pudo conectar con la base de datos. Intenta más tarde.", "error")
            return render_template("auth/registro.html", datos=datos), 503

        try:
            with conexion.cursor(cursor_factory=RealDictCursor) as cursor:
                id_cliente = obtener_siguiente_id(cursor, "cliente", "id_cliente")
                id_email = obtener_siguiente_id(cursor, "email_cliente", "id_email")
                id_usuario = obtener_siguiente_id(cursor, "usuario_sistema", "id_usuario")
                cursor.execute(
                    "INSERT INTO comercial.cliente (id_cliente, nombre, telefono) VALUES (%s, %s, %s)",
                    (id_cliente, datos["nombre"], datos["telefono"] or None),
                )
                cursor.execute(
                    """INSERT INTO comercial.email_cliente (id_email, id_cliente, email)
                       VALUES (%s, %s, %s)""",
                    (id_email, id_cliente, datos["email"]),
                )
                direccion = request.form.get("direccion", "").strip()
                if direccion:
                    id_direccion = obtener_siguiente_id(cursor, "direccion_cliente", "id_direccion")
                    cursor.execute(
                        """INSERT INTO comercial.direccion_cliente
                               (id_direccion, id_cliente, direccion)
                           VALUES (%s, %s, %s)""",
                        (id_direccion, id_cliente, direccion),
                    )
                cursor.execute(
                    """INSERT INTO comercial.usuario_sistema
                           (id_usuario, usuario, password_hash, rol, id_cliente)
                       VALUES (%s, %s, %s, 'CLIENTE', %s)""",
                    (id_usuario, datos["usuario"], generate_password_hash(password), id_cliente),
                )
            conexion.commit()
        except psycopg2.errors.UniqueViolation:
            conexion.rollback()
            flash("Ese nombre de usuario o correo ya está registrado.", "error")
            return render_template("auth/registro.html", datos=datos), 409
        except psycopg2.Error:
            conexion.rollback()
            raise
        finally:
            conexion.close()

        flash("Cuenta creada. Ya puedes iniciar sesión.", "exito")
        return redirect(url_for("auth.login"))

    return render_template("auth/registro.html", datos=datos)


@auth_bp.route("/mi-cuenta", methods=["GET", "POST"])
@cliente_requerido
def mi_cuenta():
    conexion = obtener_conexion()
    if conexion is None:
        flash("No se pudo consultar tu cuenta.", "error")
        return redirect(url_for("tienda.catalogo"))

    try:
        with conexion.cursor(cursor_factory=RealDictCursor) as cursor:
            cursor.execute(
                """SELECT c.nombre, c.telefono, u.usuario, u.password_hash,
                          (SELECT email FROM comercial.email_cliente
                           WHERE id_cliente = c.id_cliente ORDER BY id_email LIMIT 1) AS email
                   FROM comercial.cliente c
                   JOIN comercial.usuario_sistema u ON u.id_cliente = c.id_cliente
                   WHERE c.id_cliente = %s""",
                (session["id_cliente"],),
            )
            cliente = cursor.fetchone()
            cursor.execute(
                """SELECT id_direccion, direccion FROM comercial.direccion_cliente
                   WHERE id_cliente = %s ORDER BY id_direccion""",
                (session["id_cliente"],),
            )
            direcciones = cursor.fetchall()
    finally:
        conexion.close()

    if not cliente:
        flash("No se encontró el perfil asociado a esta cuenta.", "error")
        return redirect(url_for("auth.logout"))

    if request.method == "POST":
        nombre = request.form.get("nombre", "").strip()
        telefono = request.form.get("telefono", "").strip()
        email = request.form.get("email", "").strip().lower()
        usuario = request.form.get("usuario", "").strip()
        current_password = request.form.get("current_password", "")
        new_password = request.form.get("new_password", "")
        errores = []
        if not nombre or len(nombre) > 150:
            errores.append("El nombre es obligatorio y no debe superar 150 caracteres.")
        if len(telefono) > 30:
            errores.append("El teléfono no puede superar 30 caracteres.")
        if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email) or len(email) > 254:
            errores.append("Ingresa un correo electrónico válido.")
        if not usuario or len(usuario) > 60:
            errores.append("El nombre de usuario debe tener entre 1 y 60 caracteres.")
        if new_password and (len(new_password) < 8 or not check_password_hash(
            cliente["password_hash"], current_password
        )):
            errores.append("Para cambiar la contraseña, ingresa tu contraseña actual y una nueva de al menos 8 caracteres.")

        if errores:
            for error in errores:
                flash(error, "error")
            cliente.update(nombre=nombre, telefono=telefono, email=email, usuario=usuario)
            return render_template("auth/mi_cuenta.html", cliente=cliente, direcciones=direcciones), 400

        conexion = obtener_conexion()
        if conexion is None:
            flash("No se pudo actualizar tu cuenta.", "error")
            return redirect(url_for("auth.mi_cuenta"))

        try:
            with conexion.cursor(cursor_factory=RealDictCursor) as cursor:
                cursor.execute(
                    """SELECT id_email FROM comercial.email_cliente
                       WHERE id_cliente = %s ORDER BY id_email LIMIT 1""",
                    (session["id_cliente"],),
                )
                fila_email = cursor.fetchone()
                cursor.execute(
                    "UPDATE comercial.cliente SET nombre = %s, telefono = %s WHERE id_cliente = %s",
                    (nombre, telefono or None, session["id_cliente"]),
                )
                cursor.execute(
                    "UPDATE comercial.usuario_sistema SET usuario = %s WHERE id_usuario = %s",
                    (usuario, session["id_usuario"]),
                )
                if new_password:
                    cursor.execute(
                        "UPDATE comercial.usuario_sistema SET password_hash = %s WHERE id_usuario = %s",
                        (generate_password_hash(new_password), session["id_usuario"]),
                    )
                if fila_email:
                    cursor.execute(
                        "UPDATE comercial.email_cliente SET email = %s WHERE id_email = %s",
                        (email, fila_email["id_email"]),
                    )
                else:
                    id_email = obtener_siguiente_id(cursor, "email_cliente", "id_email")
                    cursor.execute(
                        """INSERT INTO comercial.email_cliente (id_email, id_cliente, email)
                           VALUES (%s, %s, %s)""",
                        (id_email, session["id_cliente"], email),
                    )
            conexion.commit()
        except psycopg2.errors.UniqueViolation:
            conexion.rollback()
            flash("El usuario o correo ya está registrado por otra cuenta.", "error")
            return redirect(url_for("auth.mi_cuenta"))
        except psycopg2.Error:
            conexion.rollback()
            raise
        finally:
            conexion.close()

        session["usuario"] = usuario
        flash("Tu cuenta fue actualizada.", "exito")
        return redirect(url_for("auth.mi_cuenta"))

    return render_template("auth/mi_cuenta.html", cliente=cliente, direcciones=direcciones)


@auth_bp.route("/mi-cuenta/direcciones", methods=["POST"])
@cliente_requerido
def agregar_direccion():
    direccion = request.form.get("direccion", "").strip()
    if not direccion or len(direccion) > 255:
        flash("La dirección es obligatoria y no debe superar 255 caracteres.", "error")
        return redirect(url_for("auth.mi_cuenta"))
    conexion = obtener_conexion()
    if conexion is None:
        flash("No se pudo guardar la dirección.", "error")
        return redirect(url_for("auth.mi_cuenta"))
    try:
        with conexion.cursor() as cursor:
            id_direccion = obtener_siguiente_id(cursor, "direccion_cliente", "id_direccion")
            cursor.execute(
                """INSERT INTO comercial.direccion_cliente (id_direccion, id_cliente, direccion)
                   VALUES (%s, %s, %s)""",
                (id_direccion, session["id_cliente"], direccion),
            )
        conexion.commit()
    except psycopg2.errors.UniqueViolation:
        conexion.rollback()
        flash("Esa dirección ya está guardada en tu cuenta.", "error")
        return redirect(url_for("auth.mi_cuenta"))
    except psycopg2.Error:
        conexion.rollback()
        raise
    finally:
        conexion.close()
    flash("Dirección agregada a tu cuenta.", "exito")
    return redirect(url_for("auth.mi_cuenta"))


@auth_bp.route("/mi-cuenta/direcciones/<int:id_direccion>/eliminar", methods=["POST"])
@cliente_requerido
def eliminar_direccion(id_direccion):
    conexion = obtener_conexion()
    if conexion is None:
        flash("No se pudo eliminar la dirección.", "error")
        return redirect(url_for("auth.mi_cuenta"))
    try:
        with conexion.cursor() as cursor:
            cursor.execute(
                """DELETE FROM comercial.direccion_cliente
                   WHERE id_direccion = %s AND id_cliente = %s""",
                (id_direccion, session["id_cliente"]),
            )
            eliminada = cursor.rowcount
        conexion.commit()
    except psycopg2.Error:
        conexion.rollback()
        raise
    finally:
        conexion.close()
    flash("Dirección eliminada." if eliminada else "No se encontró esa dirección.", "exito" if eliminada else "error")
    return redirect(url_for("auth.mi_cuenta"))


@auth_bp.route("/usuarios")
@administrador_requerido
def usuarios():
    conexion = obtener_conexion()
    if conexion is None:
        flash("No se pudo consultar la gestión de usuarios.", "error")
        return render_template("auth/usuarios.html", usuarios=[]), 503

    try:
        with conexion.cursor(cursor_factory=RealDictCursor) as cursor:
            cursor.execute(
                """SELECT u.id_usuario, u.usuario, u.rol, u.activo,
                          COALESCE(c.nombre, v.nombre || ' ' || v.apellido, u.usuario) AS nombre,
                          ec.email
                   FROM comercial.usuario_sistema u
                   LEFT JOIN comercial.cliente c ON c.id_cliente = u.id_cliente
                   LEFT JOIN comercial.vendedor v ON v.id_vendedor = u.id_vendedor
                   LEFT JOIN LATERAL (
                       SELECT email FROM comercial.email_cliente
                       WHERE id_cliente = u.id_cliente ORDER BY id_email LIMIT 1
                   ) ec ON TRUE
                   ORDER BY u.rol, u.usuario"""
            )
            cuentas = cursor.fetchall()
    finally:
        conexion.close()

    return render_template("auth/usuarios.html", usuarios=cuentas)


@auth_bp.route("/usuarios/nuevo", methods=["GET", "POST"])
@administrador_requerido
def nuevo_usuario():
    datos = _datos_usuario_staff()
    if request.method == "POST":
        rol = request.form.get("rol", "")
        password = request.form.get("password", "")
        errores = _validar_usuario_staff(datos, rol)
        if len(password) < 8:
            errores.append("La contraseña debe tener al menos 8 caracteres.")

        if errores:
            for error in errores:
                flash(error, "error")
            return render_template("auth/usuario_formulario.html", datos=datos, rol=rol), 400

        conexion = obtener_conexion()
        if conexion is None:
            flash("No se pudo conectar con la base de datos.", "error")
            return render_template("auth/usuario_formulario.html", datos=datos, rol=rol), 503

        try:
            with conexion.cursor(cursor_factory=RealDictCursor) as cursor:
                id_usuario = obtener_siguiente_id(cursor, "usuario_sistema", "id_usuario")
                id_vendedor = None
                if rol == "VENDEDOR":
                    id_vendedor = obtener_siguiente_id(cursor, "vendedor", "id_vendedor")
                    cursor.execute(
                        """INSERT INTO comercial.vendedor
                               (id_vendedor, nombre, apellido, ci, telefono, email)
                           VALUES (%s, %s, %s, %s, %s, %s)""",
                        (id_vendedor, datos["nombre"], datos["apellido"], datos["ci"] or None,
                         datos["telefono"] or None, datos["email"] or None),
                    )
                cursor.execute(
                    """INSERT INTO comercial.usuario_sistema
                           (id_usuario, usuario, password_hash, rol, id_vendedor)
                       VALUES (%s, %s, %s, %s, %s)""",
                    (id_usuario, datos["usuario"], generate_password_hash(password), rol, id_vendedor),
                )
            conexion.commit()
        except psycopg2.errors.UniqueViolation:
            conexion.rollback()
            flash("Ese nombre de usuario, CI o correo ya está registrado.", "error")
            return render_template("auth/usuario_formulario.html", datos=datos, rol=rol), 409
        except psycopg2.Error:
            conexion.rollback()
            raise
        finally:
            conexion.close()

        flash("Usuario creado correctamente.", "exito")
        return redirect(url_for("auth.usuarios"))

    return render_template("auth/usuario_formulario.html", datos=datos, rol="VENDEDOR")


@auth_bp.route("/usuarios/<int:id_usuario>/editar", methods=["GET", "POST"])
@administrador_requerido
def editar_usuario(id_usuario):
    conexion = obtener_conexion()
    if conexion is None:
        flash("No se pudo consultar la cuenta.", "error")
        return redirect(url_for("auth.usuarios"))

    try:
        with conexion.cursor(cursor_factory=RealDictCursor) as cursor:
            cursor.execute(
                """SELECT u.id_usuario, u.usuario, u.rol, u.activo, u.id_vendedor,
                          v.nombre, v.apellido, v.ci, v.telefono, v.email
                   FROM comercial.usuario_sistema u
                   LEFT JOIN comercial.vendedor v ON v.id_vendedor = u.id_vendedor
                   WHERE u.id_usuario = %s""",
                (id_usuario,),
            )
            cuenta = cursor.fetchone()
    finally:
        conexion.close()

    if not cuenta or cuenta["rol"] not in ("ADMINISTRADOR", "VENDEDOR"):
        flash("Solo se pueden editar cuentas de administrador o vendedor.", "error")
        return redirect(url_for("auth.usuarios"))

    datos = {
        "usuario": request.form.get("usuario", cuenta["usuario"]).strip(),
        "nombre": request.form.get("nombre", cuenta["nombre"] or "").strip(),
        "apellido": request.form.get("apellido", cuenta["apellido"] or "").strip(),
        "ci": request.form.get("ci", cuenta["ci"] or "").strip(),
        "telefono": request.form.get("telefono", cuenta["telefono"] or "").strip(),
        "email": request.form.get("email", cuenta["email"] or "").strip().lower(),
    }

    if request.method == "POST":
        password = request.form.get("password", "")
        errores = _validar_usuario_staff(datos, cuenta["rol"])
        if password and len(password) < 8:
            errores.append("La contraseña debe tener al menos 8 caracteres.")
        if errores:
            for error in errores:
                flash(error, "error")
            return render_template(
                "auth/usuario_formulario.html", datos=datos, rol=cuenta["rol"], editando=True
            ), 400

        conexion = obtener_conexion()
        if conexion is None:
            flash("No se pudo conectar con la base de datos.", "error")
            return render_template(
                "auth/usuario_formulario.html", datos=datos, rol=cuenta["rol"], editando=True
            ), 503

        try:
            with conexion.cursor() as cursor:
                if password:
                    cursor.execute(
                        """UPDATE comercial.usuario_sistema
                           SET usuario = %s, password_hash = %s WHERE id_usuario = %s""",
                        (datos["usuario"], generate_password_hash(password), id_usuario),
                    )
                else:
                    cursor.execute(
                        "UPDATE comercial.usuario_sistema SET usuario = %s WHERE id_usuario = %s",
                        (datos["usuario"], id_usuario),
                    )
                if cuenta["rol"] == "VENDEDOR":
                    cursor.execute(
                        """UPDATE comercial.vendedor
                           SET nombre = %s, apellido = %s, ci = %s, telefono = %s, email = %s
                           WHERE id_vendedor = %s""",
                        (datos["nombre"], datos["apellido"], datos["ci"] or None,
                         datos["telefono"] or None, datos["email"] or None, cuenta["id_vendedor"]),
                    )
            conexion.commit()
        except psycopg2.errors.UniqueViolation:
            conexion.rollback()
            flash("Ese nombre de usuario, CI o correo ya está registrado.", "error")
            return render_template(
                "auth/usuario_formulario.html", datos=datos, rol=cuenta["rol"], editando=True
            ), 409
        except psycopg2.Error:
            conexion.rollback()
            raise
        finally:
            conexion.close()

        flash("Usuario actualizado correctamente.", "exito")
        return redirect(url_for("auth.usuarios"))

    return render_template(
        "auth/usuario_formulario.html", datos=datos, rol=cuenta["rol"], editando=True
    )


@auth_bp.route("/usuarios/<int:id_usuario>/eliminar", methods=["POST"])
@administrador_requerido
def eliminar_usuario(id_usuario):
    if id_usuario == session.get("id_usuario"):
        flash("No puedes eliminar tu propia cuenta.", "error")
        return redirect(url_for("auth.usuarios"))

    conexion = obtener_conexion()
    if conexion is None:
        flash("No se pudo eliminar la cuenta.", "error")
        return redirect(url_for("auth.usuarios"))

    try:
        with conexion.cursor(cursor_factory=RealDictCursor) as cursor:
            cursor.execute(
                """SELECT rol, id_vendedor FROM comercial.usuario_sistema
                   WHERE id_usuario = %s FOR UPDATE""",
                (id_usuario,),
            )
            cuenta = cursor.fetchone()
            if not cuenta or cuenta["rol"] not in ("ADMINISTRADOR", "VENDEDOR"):
                flash("Solo se pueden eliminar cuentas de administrador o vendedor.", "error")
                return redirect(url_for("auth.usuarios"))
            cursor.execute(
                "UPDATE comercial.usuario_sistema SET activo = FALSE WHERE id_usuario = %s",
                (id_usuario,),
            )
            if cuenta["id_vendedor"] is not None:
                cursor.execute(
                    "UPDATE comercial.vendedor SET estado = 'INACTIVO' WHERE id_vendedor = %s",
                    (cuenta["id_vendedor"],),
                )
        conexion.commit()
    except psycopg2.Error:
        conexion.rollback()
        raise
    finally:
        conexion.close()

    flash("Cuenta eliminada del acceso. Se conservaron los registros relacionados.", "exito")
    return redirect(url_for("auth.usuarios"))


@auth_bp.route("/usuarios/<int:id_usuario>/estado", methods=["POST"])
@administrador_requerido
def cambiar_estado_usuario(id_usuario):
    if id_usuario == session.get("id_usuario"):
        flash("No puedes desactivar tu propia cuenta.", "error")
        return redirect(url_for("auth.usuarios"))

    conexion = obtener_conexion()
    if conexion is None:
        flash("No se pudo actualizar la cuenta.", "error")
        return redirect(url_for("auth.usuarios"))

    try:
        with conexion.cursor(cursor_factory=RealDictCursor) as cursor:
            cursor.execute(
                """SELECT activo, rol, id_vendedor FROM comercial.usuario_sistema
                   WHERE id_usuario = %s FOR UPDATE""",
                (id_usuario,),
            )
            cuenta = cursor.fetchone()
            if not cuenta:
                flash("La cuenta no existe.", "error")
                return redirect(url_for("auth.usuarios"))
            cursor.execute(
                "UPDATE comercial.usuario_sistema SET activo = %s WHERE id_usuario = %s",
                (not cuenta["activo"], id_usuario),
            )
            if cuenta["id_vendedor"] is not None:
                estado_vendedor = "ACTIVO" if not cuenta["activo"] else "INACTIVO"
                cursor.execute(
                    "UPDATE comercial.vendedor SET estado = %s WHERE id_vendedor = %s",
                    (estado_vendedor, cuenta["id_vendedor"]),
                )
        conexion.commit()
    except psycopg2.Error:
        conexion.rollback()
        raise
    finally:
        conexion.close()

    flash("Estado de la cuenta actualizado.", "exito")
    return redirect(url_for("auth.usuarios"))


def _datos_usuario_staff():
    return {
        "usuario": request.form.get("usuario", "").strip(),
        "nombre": request.form.get("nombre", "").strip(),
        "apellido": request.form.get("apellido", "").strip(),
        "ci": request.form.get("ci", "").strip(),
        "telefono": request.form.get("telefono", "").strip(),
        "email": request.form.get("email", "").strip().lower(),
    }


def _validar_usuario_staff(datos, rol):
    errores = []
    if rol not in ("ADMINISTRADOR", "VENDEDOR"):
        errores.append("Solo se pueden crear cuentas de administrador o vendedor.")
    if not datos["usuario"] or len(datos["usuario"]) > 60:
        errores.append("El usuario debe tener entre 1 y 60 caracteres.")
    if rol == "VENDEDOR":
        if not datos["nombre"] or len(datos["nombre"]) > 100:
            errores.append("El nombre del vendedor es obligatorio y no debe superar 100 caracteres.")
        if not datos["apellido"] or len(datos["apellido"]) > 100:
            errores.append("El apellido del vendedor es obligatorio y no debe superar 100 caracteres.")
        if len(datos["ci"]) > 30:
            errores.append("El CI no puede superar 30 caracteres.")
        if len(datos["telefono"]) > 30:
            errores.append("El teléfono no puede superar 30 caracteres.")
    if datos["email"] and (
        not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", datos["email"])
        or len(datos["email"]) > 254
    ):
        errores.append("Ingresa un correo electrónico válido.")
    return errores


@auth_bp.route("/logout")
def logout():
    token = session.get("carrito_token")
    id_cliente = session.get("id_cliente")
    reserva_no_liberada = False
    if token and id_cliente:
        conexion = obtener_conexion()
        if conexion is None:
            current_app.logger.error(
                "No se pudo liberar la reserva %s al cerrar sesión; vencerá automáticamente.",
                token,
            )
            reserva_no_liberada = True
        else:
            try:
                with conexion.cursor() as cursor:
                    cursor.execute(
                        """DELETE FROM comercial.reserva_carrito
                           WHERE token = %s AND id_cliente = %s""",
                        (token, id_cliente),
                    )
                conexion.commit()
            except psycopg2.Error:
                conexion.rollback()
                current_app.logger.exception(
                    "No se pudo liberar la reserva del carrito al cerrar sesión."
                )
                reserva_no_liberada = True
            finally:
                conexion.close()
    session.clear()
    if reserva_no_liberada:
        flash(
            "La reserva del carrito no pudo liberarse ahora y vencerá automáticamente.",
            "advertencia",
        )
    flash("Sesión cerrada correctamente.", "exito")
    return redirect(url_for("inicio"))
