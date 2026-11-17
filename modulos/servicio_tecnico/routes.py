from datetime import datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation

import psycopg2
from flask import Blueprint, flash, redirect, render_template, request, session, url_for
from psycopg2.extras import RealDictCursor

from db.conexion import obtener_conexion, obtener_siguiente_id
from modulos.auth.routes import cliente_requerido, empleado_requerido

servicio_tecnico_bp = Blueprint("servicio_tecnico", __name__)
ZONA_HORARIA = timezone(timedelta(hours=-4))
METODOS_PAGO = {"EFECTIVO", "TRANSFERENCIA", "QR"}
ESTADOS_SERVICIO = ("RECIBIDA", "EN_REVISION", "EN_REPARACION", "FINALIZADA", "CANCELADA")


def _slots_disponibles(cursor, ahora=None):
    ahora = ahora or datetime.now(ZONA_HORARIA)
    limite = ahora + timedelta(days=14)
    cursor.execute(
        """SELECT fecha_cita FROM comercial.orden_servicio
           WHERE fecha_cita >= %s AND fecha_cita < %s AND estado <> 'CANCELADA'""",
        (ahora, limite),
    )
    ocupados = {fila["fecha_cita"].astimezone(ZONA_HORARIA) for fila in cursor.fetchall()}
    slots = []
    for desplazamiento in range(14):
        dia = ahora.date() + timedelta(days=desplazamiento)
        if dia.weekday() == 6:
            continue
        hora = datetime.combine(dia, time(9, 0), tzinfo=ZONA_HORARIA)
        fin = datetime.combine(dia, time(17, 0), tzinfo=ZONA_HORARIA)
        while hora + timedelta(minutes=30) <= fin:
            if hora > ahora and hora not in ocupados:
                slots.append(hora)
            hora += timedelta(minutes=30)
    return slots


@servicio_tecnico_bp.route("/mantenimiento", methods=["GET", "POST"])
@cliente_requerido
def solicitar_mantenimiento():
    conexion = obtener_conexion()
    if conexion is None:
        flash("No se pudo conectar con la base de datos.", "error")
        return render_template("servicio_tecnico/solicitar.html", slots=[]), 503

    try:
        with conexion.cursor(cursor_factory=RealDictCursor) as cursor:
            slots = _slots_disponibles(cursor)
            if request.method == "POST":
                tipo = request.form.get("tipo_equipo", "")
                marca = request.form.get("marca", "").strip()
                modelo = request.form.get("modelo", "").strip()
                serie = request.form.get("serie", "").strip()
                problema = request.form.get("problema", "").strip()
                metodo_pago = request.form.get("metodo_pago", "")
                slot_texto = request.form.get("fecha_cita", "")
                errores = []
                if tipo not in ("PC", "LAPTOP"):
                    errores.append("Selecciona PC de escritorio o laptop.")
                if not marca or len(marca) > 100:
                    errores.append("Indica la marca del equipo (máximo 100 caracteres).")
                if len(modelo) > 100 or len(serie) > 120:
                    errores.append("El modelo o número de serie supera la longitud permitida.")
                if not problema or len(problema) > 4000:
                    errores.append("Describe el problema (máximo 4000 caracteres).")
                if metodo_pago not in METODOS_PAGO:
                    errores.append("Selecciona un método de pago.")
                try:
                    fecha_cita = datetime.fromisoformat(slot_texto)
                except ValueError:
                    fecha_cita = None
                if fecha_cita not in slots:
                    errores.append("Ese horario ya no está disponible. Selecciona otro.")

                if errores:
                    for error in errores:
                        flash(error, "error")
                    return render_template(
                        "servicio_tecnico/solicitar.html", slots=slots,
                        valores=request.form,
                    ), 400

                id_orden = obtener_siguiente_id(cursor, "orden_servicio", "id_orden")
                id_reparacion = obtener_siguiente_id(cursor, "reparacion", "id_reparacion")
                cursor.execute(
                    """INSERT INTO comercial.orden_servicio
                           (id_orden, estado, descripcion, fecha_cita, id_cliente,
                            tipo_equipo, marca_equipo, modelo_equipo, serie_equipo, metodo_pago)
                       VALUES (%s, 'RECIBIDA', %s, %s, %s, %s, %s, %s, %s, %s)""",
                    (
                        id_orden, problema, fecha_cita, session["id_cliente"], tipo,
                        marca, modelo or None, serie or None, metodo_pago,
                    ),
                )
                cursor.execute(
                    """INSERT INTO comercial.reparacion
                           (id_reparacion, id_orden, estado, costo)
                       VALUES (%s, %s, 'PENDIENTE', 0)""",
                    (id_reparacion, id_orden),
                )
                id_historial = obtener_siguiente_id(cursor, "historial", "id_historial")
                cursor.execute(
                    """INSERT INTO comercial.historial
                           (id_historial, id_orden, tipo_evento, descripcion)
                       VALUES (%s, %s, 'CITA_SOLICITADA', %s)""",
                    (id_historial, id_orden, f"Cita de diagnóstico solicitada para {fecha_cita.isoformat()}."),
                )
                conexion.commit()
                flash(f"Cita de diagnóstico reservada. Orden #{id_orden}. Duración: 30 minutos.", "exito")
                return redirect(url_for("servicio_tecnico.mis_mantenimientos"))
    except psycopg2.errors.UniqueViolation:
        conexion.rollback()
        flash("Ese horario acaba de ser reservado. Selecciona otro disponible.", "error")
        return redirect(url_for("servicio_tecnico.solicitar_mantenimiento"))
    except psycopg2.Error:
        conexion.rollback()
        raise
    finally:
        conexion.close()

    return render_template("servicio_tecnico/solicitar.html", slots=slots, valores={})


@servicio_tecnico_bp.route("/mis-mantenimientos")
@cliente_requerido
def mis_mantenimientos():
    conexion = obtener_conexion()
    if conexion is None:
        flash("No se pudieron consultar tus mantenimientos.", "error")
        return render_template("servicio_tecnico/mis_mantenimientos.html", ordenes=[]), 503
    try:
        with conexion.cursor(cursor_factory=RealDictCursor) as cursor:
            cursor.execute(
                """SELECT o.id_orden, o.fecha, o.fecha_cita, o.estado, o.descripcion AS problema,
                          o.tipo_equipo, o.marca_equipo, o.modelo_equipo, o.diagnostico,
                          r.descripcion AS trabajo_realizado, r.costo,
                          f.id_factura, f.serie
                   FROM comercial.orden_servicio o
                   LEFT JOIN comercial.reparacion r ON r.id_orden = o.id_orden
                   LEFT JOIN comercial.pago pago ON pago.id_orden = o.id_orden
                   LEFT JOIN comercial.factura f ON f.id_pago = pago.id_pago
                   WHERE o.id_cliente = %s
                   ORDER BY o.fecha_cita DESC NULLS LAST, o.fecha DESC""",
                (session["id_cliente"],),
            )
            ordenes = cursor.fetchall()
    finally:
        conexion.close()
    return render_template("servicio_tecnico/mis_mantenimientos.html", ordenes=ordenes)


@servicio_tecnico_bp.route("/mis-mantenimientos/factura/<int:id_factura>")
@cliente_requerido
def factura_mantenimiento_cliente(id_factura):
    try:
        factura = _obtener_factura_mantenimiento(id_factura, session["id_cliente"])
    except ConnectionError:
        flash("No se pudo conectar con la base de datos.", "error")
        return redirect(url_for("servicio_tecnico.mis_mantenimientos")), 503
    if not factura:
        flash("No se encontró esa factura de mantenimiento en tu cuenta.", "error")
        return redirect(url_for("servicio_tecnico.mis_mantenimientos"))
    return render_template("servicio_tecnico/factura.html", factura=factura, vista_cliente=True)


@servicio_tecnico_bp.route("/servicio-tecnico/<int:id_orden>/factura")
@empleado_requerido
def factura_mantenimiento_empleado(id_orden):
    conexion = obtener_conexion()
    if conexion is None:
        flash("No se pudo conectar con la base de datos.", "error")
        return redirect(url_for("servicio_tecnico.listar_mantenimientos")), 503
    try:
        with conexion.cursor(cursor_factory=RealDictCursor) as cursor:
            cursor.execute(
                """SELECT f.id_factura, o.id_cliente
                   FROM comercial.factura f
                   JOIN comercial.pago pago ON pago.id_pago = f.id_pago
                   JOIN comercial.orden_servicio o ON o.id_orden = pago.id_orden
                   WHERE o.id_orden = %s""",
                (id_orden,),
            )
            referencia = cursor.fetchone()
    finally:
        conexion.close()
    if not referencia:
        flash("La orden todavía no tiene una factura.", "error")
        return redirect(url_for("servicio_tecnico.detalle_mantenimiento", id_orden=id_orden))
    try:
        factura = _obtener_factura_mantenimiento(
            referencia["id_factura"], referencia["id_cliente"]
        )
    except ConnectionError:
        flash("No se pudo conectar con la base de datos.", "error")
        return redirect(url_for("servicio_tecnico.listar_mantenimientos")), 503
    if not factura:
        flash("No se encontró esa factura de mantenimiento.", "error")
        return redirect(url_for("servicio_tecnico.listar_mantenimientos"))
    return render_template("servicio_tecnico/factura.html", factura=factura, vista_cliente=False)


@servicio_tecnico_bp.route("/servicio-tecnico")
@empleado_requerido
def listar_mantenimientos():
    conexion = obtener_conexion()
    if conexion is None:
        flash("No se pudieron consultar las órdenes de mantenimiento.", "error")
        return render_template("servicio_tecnico/listar.html", ordenes=[]), 503
    try:
        with conexion.cursor(cursor_factory=RealDictCursor) as cursor:
            cursor.execute(
                """SELECT o.id_orden, o.fecha, o.fecha_cita, o.estado, o.tipo_equipo,
                          o.marca_equipo, o.modelo_equipo, c.nombre AS cliente,
                          u.usuario AS tecnico, r.costo, f.serie AS factura
                   FROM comercial.orden_servicio o
                   JOIN comercial.cliente c ON c.id_cliente = o.id_cliente
                   LEFT JOIN comercial.usuario_sistema u ON u.id_usuario = o.id_usuario_tecnico
                   LEFT JOIN comercial.reparacion r ON r.id_orden = o.id_orden
                   LEFT JOIN comercial.pago pago ON pago.id_orden = o.id_orden
                   LEFT JOIN comercial.factura f ON f.id_pago = pago.id_pago
                   ORDER BY o.fecha_cita DESC NULLS LAST, o.fecha DESC"""
            )
            ordenes = cursor.fetchall()
    finally:
        conexion.close()
    return render_template(
        "servicio_tecnico/listar.html", ordenes=ordenes, estados=ESTADOS_SERVICIO
    )


@servicio_tecnico_bp.route("/servicio-tecnico/<int:id_orden>", methods=["GET", "POST"])
@empleado_requerido
def detalle_mantenimiento(id_orden):
    conexion = obtener_conexion()
    if conexion is None:
        flash("No se pudo consultar la orden.", "error")
        return redirect(url_for("servicio_tecnico.listar_mantenimientos"))

    historial = []
    try:
        with conexion.cursor(cursor_factory=RealDictCursor) as cursor:
            cursor.execute(
                """SELECT o.*, c.nombre AS cliente, c.telefono,
                          r.id_reparacion, r.descripcion AS trabajo_realizado,
                          r.estado AS estado_reparacion, r.costo,
                          f.id_factura, f.serie
                   FROM comercial.orden_servicio o
                   JOIN comercial.cliente c ON c.id_cliente = o.id_cliente
                   LEFT JOIN comercial.reparacion r ON r.id_orden = o.id_orden
                   LEFT JOIN comercial.pago pago ON pago.id_orden = o.id_orden
                   LEFT JOIN comercial.factura f ON f.id_pago = pago.id_pago
                   WHERE o.id_orden = %s""",
                (id_orden,),
            )
            orden = cursor.fetchone()
            if not orden:
                flash("Orden de mantenimiento no encontrada.", "error")
                return redirect(url_for("servicio_tecnico.listar_mantenimientos"))

            if request.method == "POST":
                if orden["estado"] == "FINALIZADA":
                    flash("El mantenimiento ya fue finalizado y facturado.", "error")
                    return redirect(url_for("servicio_tecnico.detalle_mantenimiento", id_orden=id_orden))
                estado = request.form.get("estado", "")
                diagnostico = request.form.get("diagnostico", "").strip()
                trabajo = request.form.get("trabajo_realizado", "").strip()
                try:
                    costo = Decimal(request.form.get("costo", "").strip())
                except (InvalidOperation, ValueError):
                    costo = Decimal("-1")
                if estado not in ESTADOS_SERVICIO or not diagnostico or len(diagnostico) > 4000:
                    flash("Selecciona un estado y registra un diagnóstico válido.", "error")
                    return redirect(url_for("servicio_tecnico.detalle_mantenimiento", id_orden=id_orden))
                if not costo.is_finite() or costo < 0 or costo > Decimal("9999999999.99"):
                    flash("El costo debe ser un monto válido mayor o igual a cero.", "error")
                    return redirect(url_for("servicio_tecnico.detalle_mantenimiento", id_orden=id_orden))
                if estado == "FINALIZADA" and not trabajo:
                    flash("Describe el mantenimiento realizado antes de facturar.", "error")
                    return redirect(url_for("servicio_tecnico.detalle_mantenimiento", id_orden=id_orden))
                if len(trabajo) > 4000:
                    flash("La descripción del trabajo no debe superar 4000 caracteres.", "error")
                    return redirect(url_for("servicio_tecnico.detalle_mantenimiento", id_orden=id_orden))

                cursor.execute(
                    """UPDATE comercial.orden_servicio
                       SET estado = %s, diagnostico = %s, tecnico = %s,
                           id_usuario_tecnico = %s
                       WHERE id_orden = %s""",
                    (estado, diagnostico, session["usuario"], session["id_usuario"], id_orden),
                )
                cursor.execute(
                    """UPDATE comercial.reparacion
                       SET estado = %s, descripcion = %s, costo = %s,
                           fecha_inicio = COALESCE(fecha_inicio, CURRENT_DATE),
                           fecha_fin = CASE WHEN %s = 'FINALIZADA' THEN CURRENT_DATE ELSE fecha_fin END
                       WHERE id_orden = %s""",
                    (estado, trabajo or None, costo, estado, id_orden),
                )
                id_historial = obtener_siguiente_id(cursor, "historial", "id_historial")
                cursor.execute(
                    """INSERT INTO comercial.historial
                           (id_historial, id_orden, tipo_evento, descripcion, observaciones,
                            id_usuario_evento)
                       VALUES (%s, %s, %s, %s, %s, %s)""",
                    (
                        id_historial, id_orden, estado,
                        f"Diagnóstico: {diagnostico}\nMantenimiento: {trabajo or 'Pendiente'}",
                        f"Responsable: {session['usuario']}; costo: Bs {costo:.2f}",
                        session["id_usuario"],
                    ),
                )
                if session.get("id_vendedor"):
                    cursor.execute(
                        """INSERT INTO comercial.historial_vendedor (id_vendedor, id_historial)
                           VALUES (%s, %s)""",
                        (session["id_vendedor"], id_historial),
                    )

                if estado == "FINALIZADA":
                    id_pago = obtener_siguiente_id(cursor, "pago", "id_pago")
                    id_factura = obtener_siguiente_id(cursor, "factura", "id_factura")
                    cursor.execute(
                        """INSERT INTO comercial.pago
                               (id_pago, id_orden, monto, metodo, estado)
                           VALUES (%s, %s, %s, %s, 'PENDIENTE')""",
                        (id_pago, id_orden, costo, orden["metodo_pago"],),
                    )
                    cursor.execute(
                        """INSERT INTO comercial.factura (id_factura, id_pago, serie, estado)
                           VALUES (%s, %s, %s, 'EMITIDA')""",
                        (id_factura, id_pago, f"MTS-{id_factura:06d}"),
                    )
                conexion.commit()
                flash(
                    "Mantenimiento finalizado y factura emitida."
                    if estado == "FINALIZADA" else "Orden de mantenimiento actualizada.",
                    "exito",
                )
                return redirect(url_for("servicio_tecnico.detalle_mantenimiento", id_orden=id_orden))
            cursor.execute(
                """SELECT h.fecha_evento, h.tipo_evento, h.descripcion, h.observaciones,
                          COALESCE(ue.usuario, uv.usuario) AS responsable
                   FROM comercial.historial h
                   LEFT JOIN comercial.historial_vendedor hv ON hv.id_historial = h.id_historial
                   LEFT JOIN comercial.usuario_sistema uv ON uv.id_vendedor = hv.id_vendedor
                   LEFT JOIN comercial.usuario_sistema ue ON ue.id_usuario = h.id_usuario_evento
                   WHERE h.id_orden = %s ORDER BY h.fecha_evento DESC""",
                (id_orden,),
            )
            historial = cursor.fetchall()
    except psycopg2.Error:
        conexion.rollback()
        raise
    finally:
        conexion.close()

    return render_template(
        "servicio_tecnico/detalle.html", orden=orden, estados=ESTADOS_SERVICIO,
        historial=historial,
    )


def _obtener_factura_mantenimiento(id_factura, id_cliente):
    conexion = obtener_conexion()
    if conexion is None:
        raise ConnectionError("Database connection unavailable")
    try:
        with conexion.cursor(cursor_factory=RealDictCursor) as cursor:
            cursor.execute(
                """SELECT f.id_factura, f.serie, f.fecha,
                          pago.monto, pago.metodo, pago.estado AS estado_pago,
                          o.id_orden, o.fecha_cita, o.tipo_equipo, o.marca_equipo,
                          o.modelo_equipo, o.descripcion AS problema, o.diagnostico,
                          o.tecnico, c.nombre AS cliente, c.telefono,
                          (SELECT email FROM comercial.email_cliente
                           WHERE id_cliente = c.id_cliente ORDER BY id_email LIMIT 1) AS email,
                          r.descripcion AS trabajo_realizado
                   FROM comercial.factura f
                   JOIN comercial.pago pago ON pago.id_pago = f.id_pago
                   JOIN comercial.orden_servicio o ON o.id_orden = pago.id_orden
                   JOIN comercial.cliente c ON c.id_cliente = o.id_cliente
                   LEFT JOIN comercial.reparacion r ON r.id_orden = o.id_orden
                   WHERE f.id_factura = %s AND o.id_cliente = %s""",
                (id_factura, id_cliente),
            )
            return cursor.fetchone()
    finally:
        conexion.close()


@servicio_tecnico_bp.route("/garantia", methods=["GET", "POST"])
@empleado_requerido
def garantia():
    conexion = obtener_conexion()
    if conexion is None:
        flash("No se pudo conectar con la base de datos.", "error")
        return render_template("servicio_tecnico/garantia.html", equipo=None), 503

    try:
        with conexion.cursor(cursor_factory=RealDictCursor) as cursor:
            equipo = None
            if request.method == "POST":
                serie = request.form.get("serie", "").strip()
                cursor.execute(
                    """SELECT c.nombre, e.marca, e.modelo, e.fecha_compra,
                              (CURRENT_DATE - e.fecha_compra) AS dias
                       FROM comercial.equipo_cliente e
                       JOIN comercial.cliente c ON e.id_cliente = c.id_cliente
                       WHERE e.serie = %s""",
                    (serie,),
                )
                equipo = cursor.fetchone()
                if equipo and equipo["dias"] is not None and equipo["dias"] <= 365:
                    flash("Garantía VIGENTE (menos de 1 año).", "exito")
                elif equipo:
                    flash("Garantía VENCIDA.", "error")
                else:
                    flash("Número de serie no encontrado.", "error")
    finally:
        conexion.close()
    return render_template("servicio_tecnico/garantia.html", equipo=equipo)
