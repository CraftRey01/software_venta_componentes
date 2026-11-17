from flask import Blueprint, flash, redirect, render_template, request, session, url_for
from decimal import Decimal
from psycopg2.extras import RealDictCursor
from uuid import uuid4

from config import Config
from db.conexion import (
    devolver_conexion_catalogo,
    obtener_conexion,
    obtener_conexion_catalogo,
    obtener_siguiente_id,
)
from modulos.auth.routes import cliente_requerido

tienda_bp = Blueprint("tienda", __name__)
MINUTOS_RESERVA_CARRITO = 15

ETIQUETAS_CORTAS = {
    "tamaño de la memoria de la computadora": "Tamano",
    "tama?o de la memoria de la computadora": "Tamano",
    "tamaño": "Tamano",
    "tama?o": "Tamano",
    "tecnología de memoria ram": "Tipo",
    "tecnolog?a de memoria ram": "Tipo",
    "tecnología de almacenamiento": "Tipo",
    "tecnolog?a de almacenamiento": "Tipo",
    "velocidad de memoria": "Velocidad",
    "memoria gráfica": "Memoria",
    "memoria gr?fica": "Memoria",
    "certificación": "Certificacion",
    "certificaci?n": "Certificacion",
}


def _preparar_caracteristicas(productos):
    for producto in productos:
        especificaciones = []
        descripcion_libre = []
        for linea in (producto.get("descripcion") or "").splitlines():
            linea = linea.strip()
            separador = ":" if ":" in linea else "\t" if "\t" in linea else None
            if separador:
                atributo, valor = (parte.strip() for parte in linea.split(separador, 1))
                if atributo and valor:
                    atributo_corto = ETIQUETAS_CORTAS.get(atributo.casefold(), atributo.rstrip(" .:"))
                    if atributo_corto.casefold() != "marca":
                        especificaciones.append({"atributo": atributo_corto, "valor": valor})
                    continue
            if linea:
                descripcion_libre.append(linea)

        marca = (producto.get("marca") or "").strip()
        producto["caracteristicas"] = (
            [{"atributo": "Marca", "valor": marca}] if marca else []
        ) + especificaciones
        producto["descripcion_adicional"] = "\n".join(descripcion_libre)


def _cantidades_sesion():
    carrito = session.get("carrito", {})
    if not isinstance(carrito, dict):
        return {}
    cantidades = {}
    for producto, cantidad in carrito.items():
        try:
            id_producto = int(producto)
            cantidad = int(cantidad)
        except (TypeError, ValueError):
            continue
        if id_producto > 0 and cantidad > 0:
            cantidades[id_producto] = cantidad
    return cantidades


def _bloquear_carrito(cursor, token):
    cursor.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (token,))


def _limpiar_reservas_vencidas(cursor):
    cursor.execute("DELETE FROM comercial.reserva_carrito WHERE vence_en <= statement_timestamp()")


def _leer_reservas_carrito(cursor, token, id_cliente):
    cursor.execute(
        """SELECT id_producto, cantidad
           FROM comercial.reserva_carrito
           WHERE token = %s AND id_cliente = %s AND vence_en > statement_timestamp()
           ORDER BY id_producto
           FOR UPDATE""",
        (token, id_cliente),
    )
    return {fila["id_producto"]: fila["cantidad"] for fila in cursor.fetchall()}


def _guardar_reservas_carrito(cursor, token, id_cliente, cantidades):
    for id_producto, cantidad in sorted(cantidades.items()):
        cursor.execute(
            """SELECT p.nombre, i.cantidad AS stock
               FROM comercial.producto p
               JOIN comercial.inventario i ON i.id_producto = p.id_producto
               WHERE p.id_producto = %s AND p.estado = 'ACTIVO'
                     AND i.estado = 'ACTIVO'
               FOR UPDATE OF p, i""",
            (id_producto,),
        )
        producto = cursor.fetchone()
        if not producto:
            raise ValueError("Uno de los productos del carrito ya no está disponible.")

        cursor.execute(
            """SELECT COALESCE(SUM(cantidad), 0) AS reservado
               FROM comercial.reserva_carrito
               WHERE id_producto = %s AND token <> %s
                     AND vence_en > statement_timestamp()""",
            (id_producto, token),
        )
        reservado_por_otros = cursor.fetchone()["reservado"]
        if cantidad > producto["stock"] - reservado_por_otros:
            disponible = max(producto["stock"] - reservado_por_otros, 0)
            raise ValueError(
                f"Solo quedan {disponible} unidades disponibles de {producto['nombre']}."
            )

    cursor.execute("DELETE FROM comercial.reserva_carrito WHERE token = %s", (token,))
    for id_producto, cantidad in sorted(cantidades.items()):
        cursor.execute(
            """INSERT INTO comercial.reserva_carrito
                   (token, id_cliente, id_producto, cantidad, reservado_en, vence_en)
               VALUES (%s, %s, %s, %s, clock_timestamp(),
                       clock_timestamp() + (%s * INTERVAL '1 minute'))""",
            (token, id_cliente, id_producto, cantidad, MINUTOS_RESERVA_CARRITO),
        )


def _normalizar_carrito(cantidades):
    return {str(id_producto): cantidad for id_producto, cantidad in cantidades.items()}


@tienda_bp.route("/catalogo")
def catalogo():
    categoria = request.args.get("categoria", type=int)
    busqueda = request.args.get("q", "").strip()
    conexion = obtener_conexion_catalogo()
    if conexion is None:
        flash("La tienda no puede conectarse a la base de datos.", "error")
        return render_template(
            "tienda/catalogo.html", productos=[], categorias=[], categoria_seleccionada=categoria,
            busqueda=busqueda, error_catalogo=True,
        ), 503

    try:
        with conexion.cursor(cursor_factory=RealDictCursor) as cursor:
            token = session.get("carrito_token")
            id_cliente = session.get("id_cliente") if session.get("rol") == "CLIENTE" else None
            if token and id_cliente:
                cursor.execute(
                    """SELECT 1 FROM comercial.reserva_carrito
                       WHERE token = %s AND id_cliente = %s
                             AND vence_en > statement_timestamp()
                       LIMIT 1""",
                    (token, id_cliente),
                )
                if not cursor.fetchone():
                    session.pop("carrito_token", None)
                    session.pop("carrito", None)
                    flash("La reserva del carrito venció. Agrega nuevamente los productos.", "advertencia")
            sql = """WITH reservas AS (
                         SELECT id_producto, SUM(cantidad) AS cantidad
                         FROM comercial.reserva_carrito
                         WHERE vence_en > statement_timestamp()
                         GROUP BY id_producto
                     )
                     SELECT vp.id_producto, vp.nombre, vp.categoria, vp.precio,
                            vp.precio_oferta,
                            vp.stock - COALESCE(reservas.cantidad, 0) AS stock,
                            vp.estado, vp.imagen_url,
                            p.marca, p.descripcion
                     FROM comercial.vista_producto_categoria vp
                     JOIN comercial.producto p ON p.id_producto = vp.id_producto
                     LEFT JOIN reservas ON reservas.id_producto = vp.id_producto
                     WHERE vp.estado = 'ACTIVO'
                           AND vp.stock - COALESCE(reservas.cantidad, 0) > 0"""
            parametros = []
            if categoria:
                sql += " AND vp.id_categoria = %s"
                parametros.append(categoria)
            if busqueda:
                sql += """ AND (vp.nombre ILIKE %s OR vp.categoria ILIKE %s
                                 OR COALESCE(p.marca, '') ILIKE %s
                                 OR COALESCE(p.descripcion, '') ILIKE %s)"""
                parametros.extend((f"%{busqueda}%", f"%{busqueda}%", f"%{busqueda}%", f"%{busqueda}%"))
            sql += " ORDER BY (vp.precio_oferta IS NOT NULL) DESC, vp.nombre"
            cursor.execute(sql, parametros)
            productos = cursor.fetchall()
            _preparar_caracteristicas(productos)
            cursor.execute(
                "SELECT id_categoria, nombre FROM comercial.categoria WHERE estado = 'ACTIVO' ORDER BY nombre"
            )
            categorias = cursor.fetchall()
        conexion.commit()
    finally:
        devolver_conexion_catalogo(conexion)

    return render_template(
        "tienda/catalogo.html", productos=productos, categorias=categorias,
        categoria_seleccionada=categoria, busqueda=busqueda, error_catalogo=False,
    )


@tienda_bp.route("/agregar/<int:id_producto>", methods=["POST"])
@cliente_requerido
def agregar_carrito(id_producto):
    cantidad = request.form.get("cantidad", type=int)
    if not cantidad or cantidad < 1:
        flash("Indica una cantidad válida.", "error")
        return redirect(url_for("tienda.catalogo"))

    token = session.get("carrito_token") or uuid4().hex
    carrito_anterior = _cantidades_sesion() if not session.get("carrito_token") else {}
    conexion = obtener_conexion()
    if conexion is None:
        flash("No se pudo conectar con la base de datos.", "error")
        return redirect(url_for("tienda.catalogo"))

    try:
        with conexion.cursor(cursor_factory=RealDictCursor) as cursor:
            _bloquear_carrito(cursor, token)
            _limpiar_reservas_vencidas(cursor)
            cantidades = _leer_reservas_carrito(cursor, token, session["id_cliente"])
            if not cantidades and not session.get("carrito_token"):
                cantidades = carrito_anterior
            cantidades[id_producto] = cantidades.get(id_producto, 0) + cantidad
            _guardar_reservas_carrito(
                cursor, token, session["id_cliente"], cantidades
            )
        conexion.commit()
    except ValueError as error:
        conexion.rollback()
        flash(str(error), "error")
        return redirect(url_for("tienda.catalogo"))
    except Exception:
        conexion.rollback()
        raise
    finally:
        conexion.close()

    session["carrito_token"] = token
    session["carrito"] = _normalizar_carrito(cantidades)
    flash(
        f"Se reservaron temporalmente tus productos por {MINUTOS_RESERVA_CARRITO} minutos.",
        "exito",
    )
    return redirect(url_for("tienda.carrito"))


@tienda_bp.route("/carrito")
def carrito():
    productos = []
    direcciones = []
    reserva_hasta = None
    error_carrito = False
    id_cliente = session.get("id_cliente") if session.get("rol") == "CLIENTE" else None
    token = session.get("carrito_token")
    carrito_anterior = _cantidades_sesion() if not token else {}
    if token or (id_cliente and carrito_anterior):
        conexion = obtener_conexion()
        if conexion is None:
            flash("No se pudo cargar el carrito porque la base de datos no está disponible.", "error")
            return render_template("tienda/carrito.html", productos=[], total=0), 503
        try:
            with conexion.cursor(cursor_factory=RealDictCursor) as cursor:
                if id_cliente:
                    token = token or uuid4().hex
                    _bloquear_carrito(cursor, token)
                    _limpiar_reservas_vencidas(cursor)
                    cantidades = _leer_reservas_carrito(cursor, token, id_cliente)
                    if not cantidades and carrito_anterior:
                        cantidades = carrito_anterior
                        _guardar_reservas_carrito(
                            cursor, token, id_cliente, cantidades
                        )
                    if cantidades:
                        cursor.execute(
                            """SELECT p.id_producto, p.nombre, p.precio, p.precio_oferta,
                                      i.cantidad AS stock, p.imagen_url, rc.cantidad,
                                      rc.vence_en
                               FROM comercial.reserva_carrito rc
                               JOIN comercial.producto p ON p.id_producto = rc.id_producto
                               JOIN comercial.inventario i ON i.id_producto = rc.id_producto
                               WHERE rc.token = %s AND rc.id_cliente = %s
                                     AND rc.vence_en > statement_timestamp()
                               ORDER BY p.nombre""",
                            (token, id_cliente),
                        )
                        productos = cursor.fetchall()
                        if productos:
                            reserva_hasta = productos[0]["vence_en"]
                        else:
                            cantidades = {}
                    else:
                        cursor.execute(
                            "DELETE FROM comercial.reserva_carrito WHERE token = %s",
                            (token,),
                        )
                    cursor.execute(
                        """SELECT direccion FROM comercial.direccion_cliente
                           WHERE id_cliente = %s ORDER BY id_direccion""",
                        (id_cliente,),
                    )
                    direcciones = cursor.fetchall()

                    if cantidades:
                        session["carrito_token"] = token
                        session["carrito"] = _normalizar_carrito(cantidades)
                    else:
                        session.pop("carrito_token", None)
                        session.pop("carrito", None)

                    conexion.commit()
                elif token:
                    cursor.execute(
                        "DELETE FROM comercial.reserva_carrito WHERE token = %s",
                        (token,),
                    )
                    conexion.commit()

                for producto in productos:
                    producto["precio_actual"] = producto["precio_oferta"] or producto["precio"]
                    producto["subtotal"] = producto["precio_actual"] * producto["cantidad"]
        except ValueError as error:
            conexion.rollback()
            productos = []
            error_carrito = True
            session.pop("carrito_token", None)
            session.pop("carrito", None)
            flash(str(error), "advertencia")
        finally:
            conexion.close()

        if not productos and not error_carrito:
            flash("La reserva del carrito venció. Agrega nuevamente los productos.", "advertencia")

    total = sum(producto["subtotal"] for producto in productos)
    return render_template(
        "tienda/carrito.html", productos=productos, total=total, direcciones=direcciones,
        reserva_hasta=reserva_hasta,
    )


@tienda_bp.route("/carrito/quitar/<int:id_producto>", methods=["POST"])
@cliente_requerido
def quitar_del_carrito(id_producto):
    token = session.get("carrito_token")
    if not token:
        carrito = _cantidades_sesion()
        carrito.pop(id_producto, None)
        if carrito:
            session["carrito"] = _normalizar_carrito(carrito)
        else:
            session.pop("carrito", None)
        flash("Producto quitado del carrito.", "exito")
        return redirect(url_for("tienda.carrito"))

    conexion = obtener_conexion()
    if conexion is None:
        flash("No se pudo liberar la reserva del producto.", "error")
        return redirect(url_for("tienda.carrito")), 503
    try:
        with conexion.cursor(cursor_factory=RealDictCursor) as cursor:
            _bloquear_carrito(cursor, token)
            _limpiar_reservas_vencidas(cursor)
            cantidades = _leer_reservas_carrito(
                cursor, token, session["id_cliente"]
            )
            cantidades.pop(id_producto, None)
            if cantidades:
                _guardar_reservas_carrito(
                    cursor, token, session["id_cliente"], cantidades
                )
            else:
                cursor.execute(
                    "DELETE FROM comercial.reserva_carrito WHERE token = %s",
                    (token,),
                )
        conexion.commit()
    except Exception:
        conexion.rollback()
        raise
    finally:
        conexion.close()

    if cantidades:
        session["carrito"] = _normalizar_carrito(cantidades)
    else:
        session.pop("carrito_token", None)
        session.pop("carrito", None)
    flash("Producto quitado del carrito.", "exito")
    return redirect(url_for("tienda.carrito"))


@tienda_bp.route("/comprar", methods=["POST"])
@cliente_requerido
def comprar():
    token = session.get("carrito_token")
    if not token:
        flash("Tu reserva venció o el carrito está vacío. Agrega nuevamente los productos.", "error")
        return redirect(url_for("tienda.carrito"))

    modalidad = request.form.get("modalidad", "")
    destino_entrega = request.form.get("destino", "").strip()
    metodo_pago = request.form.get("metodo_pago", "")
    if modalidad not in ("RECOGIDA", "PUNTO_ENTREGA"):
        flash("Selecciona recogida en tienda o entrega en un punto indicado.", "error")
        return redirect(url_for("tienda.carrito"))
    if metodo_pago not in {"EFECTIVO", "TRANSFERENCIA", "QR"}:
        flash("Selecciona un método de pago válido.", "error")
        return redirect(url_for("tienda.carrito"))
    if modalidad == "PUNTO_ENTREGA" and not destino_entrega:
        flash("Indica la dirección o el punto donde deseas recibir el pedido.", "error")
        return redirect(url_for("tienda.carrito"))
    if modalidad == "RECOGIDA":
        destino_entrega = "Recogida en tienda TecnoParts"
    elif request.form.get("alcance_entrega", "") not in ("", "NACIONAL"):
        flash("Solo se realizan envíos nacionales dentro de Bolivia.", "error")
        return redirect(url_for("tienda.carrito"))
    pais = "Bolivia" if modalidad == "PUNTO_ENTREGA" else None
    if len(destino_entrega) > 255 or len(pais or "") > 100:
        flash("El punto de entrega no puede superar 255 caracteres.", "error")
        return redirect(url_for("tienda.carrito"))
    costo_entrega = Decimal("20.00") if modalidad == "PUNTO_ENTREGA" else Decimal("0.00")

    conexion = obtener_conexion()
    if conexion is None:
        flash("No se pudo conectar con la base de datos.", "error")
        return redirect(url_for("tienda.carrito"))

    id_pedido = None
    serie_factura = None
    reserva_vencida = False
    try:
        with conexion.cursor(cursor_factory=RealDictCursor) as cursor:
            _bloquear_carrito(cursor, token)
            _limpiar_reservas_vencidas(cursor)
            carrito_reservado = _leer_reservas_carrito(
                cursor, token, session["id_cliente"]
            )
            if not carrito_reservado:
                reserva_vencida = True
                raise ValueError("La reserva del carrito venció. Agrega nuevamente los productos.")

            lineas = []
            subtotal_productos = Decimal("0.00")
            for id_producto, cantidad in sorted(carrito_reservado.items()):
                if cantidad < 1:
                    raise ValueError("El carrito contiene una cantidad no válida.")
                cursor.execute(
                    """SELECT p.id_producto, p.nombre, p.precio, p.precio_oferta,
                              i.cantidad AS stock
                       FROM comercial.producto p
                       JOIN comercial.inventario i ON i.id_producto = p.id_producto
                       WHERE p.id_producto = %s AND p.estado = 'ACTIVO'
                             AND i.estado = 'ACTIVO'
                       FOR UPDATE OF p, i""",
                    (int(id_producto),),
                )
                producto = cursor.fetchone()
                if not producto:
                    raise ValueError("Uno de los productos del carrito ya no está disponible.")

                cursor.execute(
                    """SELECT COALESCE(SUM(cantidad), 0) AS reservado
                       FROM comercial.reserva_carrito
                       WHERE id_producto = %s AND token <> %s
                             AND vence_en > statement_timestamp()""",
                    (id_producto, token),
                )
                reservado_por_otros = cursor.fetchone()["reservado"]
                if cantidad > producto["stock"] - reservado_por_otros:
                    raise ValueError(
                        f"El stock disponible de {producto['nombre']} cambió. "
                        "Revisa nuevamente tu carrito."
                    )

                precio = producto["precio_oferta"] or producto["precio"]
                subtotal = precio * cantidad
                subtotal_productos += subtotal
                lineas.append((producto, cantidad, precio))

            total = subtotal_productos + (costo_entrega or Decimal("0.00"))
            id_carrito = obtener_siguiente_id(cursor, "carrito", "id_carrito")
            id_pedido = obtener_siguiente_id(cursor, "pedido", "id_pedido")
            id_pago = obtener_siguiente_id(cursor, "pago", "id_pago")
            id_factura = obtener_siguiente_id(cursor, "factura", "id_factura")
            id_envio = obtener_siguiente_id(cursor, "envio", "id_envio")
            serie_factura = f"TPV-{id_factura:06d}"
            cursor.execute(
                """INSERT INTO comercial.carrito (id_carrito, id_cliente, estado)
                   VALUES (%s, %s, 'CONVERTIDO')""",
                (id_carrito, session["id_cliente"]),
            )
            cursor.execute(
                """INSERT INTO comercial.pedido
                   (id_pedido, id_carrito, id_vendedor, estado, canal)
                   VALUES (%s, %s, 1, 'PENDIENTE', 'VIRTUAL')""",
                (id_pedido, id_carrito),
            )
            cursor.execute(
                """INSERT INTO comercial.pago
                   (id_pago, id_pedido, monto, metodo, estado)
                   VALUES (%s, %s, %s, %s, 'PENDIENTE')""",
                (id_pago, id_pedido, total, metodo_pago),
            )
            cursor.execute(
                """INSERT INTO comercial.factura (id_factura, id_pago, serie, estado)
                   VALUES (%s, %s, %s, 'EMITIDA')""",
                (id_factura, id_pago, serie_factura),
            )
            cursor.execute(
                """INSERT INTO comercial.envio
                   (id_envio, id_factura, destino, estado, modalidad, pais, costo)
                   VALUES (%s, %s, %s, %s, %s, %s, %s)""",
                (
                    id_envio, id_factura, destino_entrega,
                    "PENDIENTE",
                    modalidad, pais or None, costo_entrega,
                ),
            )
            for producto, cantidad, precio in lineas:
                cursor.execute(
                    """INSERT INTO comercial.detalle_pedido
                       (id_pedido, id_producto, cantidad, precio_unitario)
                       VALUES (%s, %s, %s, %s)""",
                    (id_pedido, producto["id_producto"], cantidad, precio),
                )
                cursor.execute(
                    """UPDATE comercial.inventario SET cantidad = cantidad - %s
                       WHERE id_producto = %s""",
                    (cantidad, producto["id_producto"]),
                )
            cursor.execute(
                "DELETE FROM comercial.reserva_carrito WHERE token = %s",
                (token,),
            )

        conexion.commit()
    except ValueError as error:
        conexion.rollback()
        if reserva_vencida:
            session.pop("carrito_token", None)
            session.pop("carrito", None)
        flash(str(error), "error")
        return redirect(url_for("tienda.carrito"))
    except Exception:
        conexion.rollback()
        raise
    finally:
        conexion.close()

    session.pop("carrito_token", None)
    session.pop("carrito", None)
    flash(
        f"Pedido #{id_pedido} recibido. Factura {serie_factura} disponible en Mis pedidos. "
        f"Total: Bs {total:.2f}; pago pendiente.",
        "exito",
    )
    return redirect(url_for("tienda.mis_pedidos"))


@tienda_bp.route("/mis-pedidos")
@cliente_requerido
def mis_pedidos():
    conexion = obtener_conexion()
    if conexion is None:
        flash("No se pudo consultar tus pedidos.", "error")
        return render_template("tienda/pedidos.html", pedidos=[]), 503
    try:
        with conexion.cursor(cursor_factory=RealDictCursor) as cursor:
            cursor.execute(
                """SELECT p.id_pedido, p.fecha, p.estado,
                          COALESCE(MAX(pago.monto), 0) AS total,
                          f.id_factura, f.serie AS factura,
                          e.modalidad AS modalidad_entrega, e.destino,
                          e.pais
                   FROM comercial.pedido p
                   JOIN comercial.carrito c ON c.id_carrito = p.id_carrito
                   LEFT JOIN comercial.detalle_pedido d ON d.id_pedido = p.id_pedido
                   LEFT JOIN comercial.pago pago ON pago.id_pedido = p.id_pedido
                   LEFT JOIN comercial.factura f ON f.id_pago = pago.id_pago
                   LEFT JOIN comercial.envio e ON e.id_factura = f.id_factura
                   WHERE c.id_cliente = %s
                   GROUP BY p.id_pedido, f.id_factura, f.serie, e.modalidad, e.destino,
                            e.pais
                   ORDER BY p.fecha DESC""",
                (session["id_cliente"],),
            )
            pedidos = cursor.fetchall()
    finally:
        conexion.close()
    return render_template("tienda/pedidos.html", pedidos=pedidos)


@tienda_bp.route("/mis-pedidos/factura/<int:id_factura>")
@cliente_requerido
def factura_cliente(id_factura):
    conexion = obtener_conexion()
    if conexion is None:
        flash("No se pudo consultar la factura.", "error")
        return redirect(url_for("tienda.mis_pedidos"))
    try:
        with conexion.cursor(cursor_factory=RealDictCursor) as cursor:
            cursor.execute(
                """SELECT f.id_factura, f.serie, f.fecha, f.estado AS estado_factura,
                          pago.monto, pago.metodo, pago.estado AS estado_pago,
                          p.id_pedido, p.fecha AS fecha_pedido, c.nombre AS cliente,
                          c.telefono, v.nombre || ' ' || v.apellido AS vendedor,
                          e.modalidad AS modalidad_entrega, e.destino,
                          e.pais, e.costo AS costo_entrega,
                          (SELECT ec.email FROM comercial.email_cliente ec
                           WHERE ec.id_cliente = c.id_cliente ORDER BY ec.id_email LIMIT 1) AS email
                   FROM comercial.factura f
                   JOIN comercial.pago pago ON pago.id_pago = f.id_pago
                   JOIN comercial.pedido p ON p.id_pedido = pago.id_pedido
                   JOIN comercial.carrito car ON car.id_carrito = p.id_carrito
                   JOIN comercial.cliente c ON c.id_cliente = car.id_cliente
                   JOIN comercial.vendedor v ON v.id_vendedor = p.id_vendedor
                   LEFT JOIN comercial.envio e ON e.id_factura = f.id_factura
                   WHERE f.id_factura = %s AND car.id_cliente = %s""",
                (id_factura, session["id_cliente"]),
            )
            factura = cursor.fetchone()
            if not factura:
                flash("No se encontró esa factura en tu cuenta.", "error")
                return redirect(url_for("tienda.mis_pedidos"))
            cursor.execute(
                """SELECT dp.cantidad, dp.precio_unitario,
                          dp.cantidad * dp.precio_unitario AS subtotal, pr.nombre
                   FROM comercial.detalle_pedido dp
                   JOIN comercial.producto pr ON pr.id_producto = dp.id_producto
                   JOIN comercial.pago pago ON pago.id_pedido = dp.id_pedido
                   JOIN comercial.factura f ON f.id_pago = pago.id_pago
                   WHERE f.id_factura = %s ORDER BY pr.nombre""",
                (id_factura,),
            )
            lineas = cursor.fetchall()
    finally:
        conexion.close()
    return render_template(
        "ventas/factura_detalle.html",
        factura=factura,
        lineas=lineas,
        vista_cliente=True,
    )
