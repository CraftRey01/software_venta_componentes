import json
from decimal import Decimal

import psycopg2
from flask import Blueprint, flash, redirect, render_template, request, session, url_for
from psycopg2.extras import RealDictCursor

from db.conexion import obtener_conexion, obtener_siguiente_id
from modulos.auth.routes import empleado_requerido

ventas_bp = Blueprint("ventas", __name__)


@ventas_bp.route("/punto_venta", methods=["GET", "POST"])
@empleado_requerido
def punto_venta():
    conexion = obtener_conexion()
    if conexion is None:
        flash("No se pudo conectar con la base de datos.", "error")
        return render_template("ventas/punto_venta.html", productos=[], clientes=[]), 503

    if request.method == "POST":
        try:
            id_cliente = request.form.get("id_cliente", type=int)
            lineas_pedido = json.loads(request.form.get("lineas", "[]"))
            if not id_cliente or not isinstance(lineas_pedido, list) or not lineas_pedido:
                raise ValueError("Selecciona un cliente y agrega al menos un producto.")
            if len(lineas_pedido) > 100:
                raise ValueError("La venta supera el límite de productos por operación.")

            cantidades = {}
            for linea in lineas_pedido:
                if not isinstance(linea, dict):
                    raise ValueError("El detalle de venta no es válido.")
                id_producto = linea.get("id_producto")
                cantidad = linea.get("cantidad")
                if (isinstance(id_producto, bool) or not isinstance(id_producto, int)
                        or isinstance(cantidad, bool) or not isinstance(cantidad, int)
                        or id_producto < 1 or cantidad < 1):
                    raise ValueError("Cada producto debe tener una cantidad válida.")
                cantidades[id_producto] = cantidades.get(id_producto, 0) + cantidad
            if len(cantidades) > 100:
                raise ValueError("La venta supera el límite de productos distintos.")

            with conexion.cursor(cursor_factory=RealDictCursor) as cursor:
                cursor.execute(
                    "SELECT id_cliente FROM comercial.cliente WHERE id_cliente = %s AND estado = 'ACTIVO'",
                    (id_cliente,),
                )
                if not cursor.fetchone():
                    raise ValueError("Selecciona un cliente activo.")

                productos_venta = []
                total = 0
                for id_producto, cantidad in sorted(cantidades.items()):
                    cursor.execute(
                        """SELECT p.id_producto, p.nombre, p.precio, i.cantidad AS stock
                           FROM comercial.producto p
                           JOIN comercial.inventario i ON i.id_producto = p.id_producto
                           WHERE p.id_producto = %s AND p.estado = 'ACTIVO'
                                 AND i.estado = 'ACTIVO'
                           FOR UPDATE OF p, i""",
                        (id_producto,),
                    )
                    producto = cursor.fetchone()
                    if not producto:
                        raise ValueError(f"Stock insuficiente o producto inactivo: #{id_producto}.")
                    cursor.execute(
                        """SELECT COALESCE(SUM(cantidad), 0) AS reservado
                           FROM comercial.reserva_carrito
                           WHERE id_producto = %s AND vence_en > statement_timestamp()""",
                        (id_producto,),
                    )
                    disponible = max(producto["stock"] - cursor.fetchone()["reservado"], 0)
                    if cantidad > disponible:
                        raise ValueError(
                            f"Stock insuficiente o producto inactivo: {producto['nombre']}."
                        )
                    producto["stock"] = disponible
                    producto["cantidad"] = cantidad
                    producto["subtotal"] = producto["precio"] * cantidad
                    productos_venta.append(producto)
                    total += producto["subtotal"]

                id_carrito = obtener_siguiente_id(cursor, "carrito", "id_carrito")
                id_pedido = obtener_siguiente_id(cursor, "pedido", "id_pedido")
                id_pago = obtener_siguiente_id(cursor, "pago", "id_pago")
                id_factura = obtener_siguiente_id(cursor, "factura", "id_factura")
                id_vendedor = session.get("id_vendedor")
                if not id_vendedor:
                    raise ValueError("La cuenta no tiene un vendedor asignado.")

                cursor.execute(
                    """INSERT INTO comercial.carrito (id_carrito, id_cliente, estado)
                       VALUES (%s, %s, 'CONVERTIDO')""",
                    (id_carrito, id_cliente),
                )
                cursor.execute(
                    """INSERT INTO comercial.pedido
                       (id_pedido, id_carrito, id_vendedor, estado, canal)
                       VALUES (%s, %s, %s, 'PAGADO', 'FISICA')""",
                    (id_pedido, id_carrito, id_vendedor),
                )
                for producto in productos_venta:
                    cursor.execute(
                        """INSERT INTO comercial.detalle_pedido
                           (id_pedido, id_producto, cantidad, precio_unitario)
                           VALUES (%s, %s, %s, %s)""",
                        (id_pedido, producto["id_producto"], producto["cantidad"], producto["precio"]),
                    )
                    cursor.execute(
                        """UPDATE comercial.inventario SET cantidad = cantidad - %s
                           WHERE id_producto = %s""",
                        (producto["cantidad"], producto["id_producto"]),
                    )
                cursor.execute(
                    """INSERT INTO comercial.pago (id_pago, id_pedido, monto, metodo, estado)
                       VALUES (%s, %s, %s, %s, 'APROBADO')""",
                    (id_pago, id_pedido, total, request.form.get("metodo", "EFECTIVO")),
                )
                cursor.execute(
                    """INSERT INTO comercial.factura (id_factura, id_pago, serie)
                       VALUES (%s, %s, %s)""",
                    (id_factura, id_pago, f"TP-{id_factura:06d}"),
                )
            conexion.commit()
        except (ValueError, json.JSONDecodeError) as error:
            conexion.rollback()
            flash(str(error), "error")
            return redirect(url_for("ventas.punto_venta"))
        except psycopg2.Error:
            conexion.rollback()
            raise
        finally:
            conexion.close()

        flash(f"Venta #{id_pedido} registrada por Bs {total:.2f}. Factura TP-{id_factura:06d}.", "exito")
        return redirect(url_for("ventas.detalle_factura", id_factura=id_factura))

    try:
        with conexion.cursor(cursor_factory=RealDictCursor) as cursor:
            cursor.execute(
                """SELECT p.id_producto, p.nombre, p.marca, p.descripcion, p.precio,
                          c.nombre AS categoria,
                          GREATEST(i.cantidad - COALESCE(reservas.cantidad, 0), 0) AS stock
                   FROM comercial.producto p
                   JOIN comercial.inventario i ON i.id_producto = p.id_producto
                   JOIN comercial.categoria c ON c.id_categoria = p.id_categoria
                   LEFT JOIN LATERAL (
                       SELECT SUM(rc.cantidad) AS cantidad
                       FROM comercial.reserva_carrito rc
                       WHERE rc.id_producto = p.id_producto
                         AND rc.vence_en > statement_timestamp()
                   ) reservas ON TRUE
                   WHERE p.estado = 'ACTIVO' AND i.estado = 'ACTIVO'
                         AND i.cantidad - COALESCE(reservas.cantidad, 0) > 0
                   ORDER BY p.nombre"""
            )
            productos = cursor.fetchall()
            cursor.execute(
                "SELECT id_cliente, nombre FROM comercial.cliente WHERE estado = 'ACTIVO' ORDER BY nombre"
            )
            clientes = cursor.fetchall()
    finally:
        conexion.close()
    return render_template("ventas/punto_venta.html", productos=productos, clientes=clientes)


@ventas_bp.route("/facturas")
@empleado_requerido
def facturas():
    conexion = obtener_conexion()
    if conexion is None:
        flash("No se pudo conectar con la base de datos.", "error")
        return render_template("ventas/facturas.html", facturas=[]), 503
    try:
        with conexion.cursor(cursor_factory=RealDictCursor) as cursor:
            cursor.execute(
                """SELECT f.id_factura, f.serie, f.fecha, f.estado AS estado_factura,
                          p.id_pedido, o.id_orden, COALESCE(p.estado, o.estado) AS estado_pedido,
                          pago.monto, c.nombre AS cliente,
                          COALESCE(v.nombre || ' ' || v.apellido, o.tecnico, 'Servicio técnico') AS vendedor,
                          CASE WHEN o.id_orden IS NULL THEN 'VENTA' ELSE 'MANTENIMIENTO' END AS tipo
                   FROM comercial.factura f
                   JOIN comercial.pago pago ON pago.id_pago = f.id_pago
                   LEFT JOIN comercial.pedido p ON p.id_pedido = pago.id_pedido
                   LEFT JOIN comercial.carrito car ON car.id_carrito = p.id_carrito
                   LEFT JOIN comercial.orden_servicio o ON o.id_orden = pago.id_orden
                   LEFT JOIN comercial.cliente c ON c.id_cliente = COALESCE(car.id_cliente, o.id_cliente)
                   LEFT JOIN comercial.vendedor v ON v.id_vendedor = p.id_vendedor
                   ORDER BY f.fecha DESC"""
            )
            facturas = cursor.fetchall()
    finally:
        conexion.close()
    return render_template("ventas/facturas.html", facturas=facturas)


@ventas_bp.route("/facturas/<int:id_factura>")
@empleado_requerido
def detalle_factura(id_factura):
    conexion = obtener_conexion()
    if conexion is None:
        flash("No se pudo conectar con la base de datos.", "error")
        return redirect(url_for("ventas.facturas"))
    try:
        with conexion.cursor(cursor_factory=RealDictCursor) as cursor:
            cursor.execute(
                """SELECT f.id_factura, f.serie, f.fecha, f.estado AS estado_factura,
                          pago.monto, pago.metodo, p.id_pedido, o.id_orden, p.fecha AS fecha_pedido,
                          c.nombre AS cliente, c.telefono,
                          COALESCE(v.nombre || ' ' || v.apellido, o.tecnico, 'Servicio técnico') AS vendedor,
                          pago.estado AS estado_pago, e.modalidad AS modalidad_entrega,
                          e.destino, e.pais, e.costo AS costo_entrega,
                          o.tipo_equipo, o.marca_equipo, o.modelo_equipo,
                          o.descripcion AS problema, o.diagnostico,
                          r.descripcion AS trabajo_realizado,
                          (SELECT ec.email FROM comercial.email_cliente ec
                           WHERE ec.id_cliente = c.id_cliente ORDER BY ec.id_email LIMIT 1) AS email
                   FROM comercial.factura f
                   JOIN comercial.pago pago ON pago.id_pago = f.id_pago
                   LEFT JOIN comercial.pedido p ON p.id_pedido = pago.id_pedido
                   LEFT JOIN comercial.carrito car ON car.id_carrito = p.id_carrito
                   LEFT JOIN comercial.orden_servicio o ON o.id_orden = pago.id_orden
                   LEFT JOIN comercial.reparacion r ON r.id_orden = o.id_orden
                   LEFT JOIN comercial.cliente c ON c.id_cliente = COALESCE(car.id_cliente, o.id_cliente)
                   LEFT JOIN comercial.vendedor v ON v.id_vendedor = p.id_vendedor
                   LEFT JOIN comercial.envio e ON e.id_factura = f.id_factura
                   WHERE f.id_factura = %s""",
                (id_factura,),
            )
            factura = cursor.fetchone()
            if not factura:
                flash("Factura no encontrada.", "error")
                return redirect(url_for("ventas.facturas"))
            cursor.execute(
                """SELECT COALESCE(dp.cantidad, 1) AS cantidad,
                          COALESCE(dp.precio_unitario, r.costo, pago.monto) AS precio_unitario,
                          COALESCE(dp.cantidad * dp.precio_unitario, r.costo, pago.monto) AS subtotal,
                          COALESCE(pr.nombre,
                              'Mantenimiento ' || COALESCE(o.tipo_equipo, 'equipo') || ' ' ||
                              COALESCE(o.marca_equipo, '') || ' ' || COALESCE(o.modelo_equipo, '')) AS nombre
                   FROM comercial.factura f
                   JOIN comercial.pago pago ON pago.id_pago = f.id_pago
                   LEFT JOIN comercial.detalle_pedido dp ON dp.id_pedido = pago.id_pedido
                   LEFT JOIN comercial.producto pr ON pr.id_producto = dp.id_producto
                   LEFT JOIN comercial.orden_servicio o ON o.id_orden = pago.id_orden
                   LEFT JOIN comercial.reparacion r ON r.id_orden = o.id_orden
                   WHERE f.id_factura = %s
                     AND (dp.id_producto IS NOT NULL OR o.id_orden IS NOT NULL)
                   ORDER BY nombre""",
                (id_factura,),
            )
            lineas = cursor.fetchall()
    finally:
        conexion.close()
    return render_template(
        "ventas/factura_detalle.html", factura=factura, lineas=lineas, vista_cliente=False
    )
