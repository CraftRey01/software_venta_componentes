import os
import uuid

from flask import Blueprint, current_app, flash, redirect, render_template, request, url_for
from psycopg2.extras import RealDictCursor
from werkzeug.utils import secure_filename

from db.conexion import obtener_conexion, obtener_siguiente_id
from modulos.auth.routes import administrador_requerido

productos_bp = Blueprint("productos", __name__)

MAX_IMAGE_BYTES = 5 * 1024 * 1024
IMAGE_FORMATS = {
    "jpg": (b"\xff\xd8\xff",),
    "jpeg": (b"\xff\xd8\xff",),
    "png": (b"\x89PNG\r\n\x1a\n",),
    "webp": (b"RIFF",),
}


def _leer_imagen():
    archivo = request.files.get("imagen")
    if not archivo or not archivo.filename:
        return None

    extension = secure_filename(archivo.filename).rsplit(".", 1)[-1].lower()
    firmas = IMAGE_FORMATS.get(extension)
    if not firmas:
        raise ValueError("La imagen debe ser JPG, PNG o WEBP.")

    contenido = archivo.stream.read(MAX_IMAGE_BYTES + 1)
    if len(contenido) > MAX_IMAGE_BYTES:
        raise ValueError("La imagen supera el límite de 5 MB.")
    if not any(contenido.startswith(firma) for firma in firmas):
        raise ValueError("El contenido del archivo no coincide con el formato de imagen.")
    if extension == "webp" and contenido[8:12] != b"WEBP":
        raise ValueError("El archivo WEBP no es válido.")

    nombre = f"{uuid.uuid4().hex}.{extension}"
    carpeta = os.path.join(current_app.static_folder, "uploads")
    os.makedirs(carpeta, exist_ok=True)
    destino = os.path.join(carpeta, nombre)
    with open(destino, "xb") as salida:
        salida.write(contenido)
    return f"/static/uploads/{nombre}", destino


def _datos_producto():
    return {
        "id_categoria": request.form.get("id_categoria", type=int),
        "nombre": request.form.get("nombre", "").strip(),
        "descripcion": request.form.get("descripcion", "").strip(),
        "marca": request.form.get("marca", "").strip(),
        "precio": request.form.get("precio", type=float),
        "precio_oferta": request.form.get("precio_oferta", type=float),
        "stock": request.form.get("stock", type=int),
        "estado": request.form.get("estado", "ACTIVO"),
    }


def _validar_datos(datos, requiere_stock=True):
    if not datos["id_categoria"] or not datos["nombre"] or datos["precio"] is None:
        raise ValueError("Completa categoría, nombre y precio.")
    if requiere_stock and datos["stock"] is None:
        raise ValueError("Indica la existencia inicial del producto.")
    if datos["precio"] < 0 or (requiere_stock and datos["stock"] < 0):
        raise ValueError("El precio y el stock no pueden ser negativos.")
    oferta = datos["precio_oferta"]
    if oferta is not None and (oferta < 0 or oferta >= datos["precio"]):
        raise ValueError("El precio de oferta debe ser mayor o igual a cero y menor que el precio normal.")
    if datos["estado"] not in ("ACTIVO", "INACTIVO"):
        raise ValueError("Selecciona un estado válido.")


def _categorias(cursor):
    cursor.execute(
        "SELECT id_categoria, nombre FROM comercial.categoria WHERE estado = 'ACTIVO' ORDER BY nombre"
    )
    return cursor.fetchall()


@productos_bp.route("/")
@administrador_requerido
def listar():
    busqueda = request.args.get("q", "").strip()
    conexion = obtener_conexion()
    if conexion is None:
        flash("No se pudo conectar con la base de datos.", "error")
        return render_template("productos/listar.html", productos=[], busqueda=busqueda), 503
    try:
        with conexion.cursor(cursor_factory=RealDictCursor) as cursor:
            sql = """SELECT p.id_producto, p.nombre, p.marca, p.precio, p.precio_oferta,
                            CASE WHEN i.estado = 'ACTIVO' THEN i.cantidad ELSE 0 END AS stock,
                            p.estado, p.imagen_url,
                            c.nombre AS categoria
                     FROM comercial.producto p
                     JOIN comercial.categoria c ON c.id_categoria = p.id_categoria
                     LEFT JOIN comercial.inventario i ON i.id_producto = p.id_producto"""
            parametros = ()
            if busqueda:
                sql += " WHERE p.nombre ILIKE %s OR p.marca ILIKE %s"
                parametros = (f"%{busqueda}%", f"%{busqueda}%")
            cursor.execute(sql + " ORDER BY p.nombre", parametros)
            productos = cursor.fetchall()
    finally:
        conexion.close()
    return render_template("productos/listar.html", productos=productos, busqueda=busqueda)


@productos_bp.route("/nuevo", methods=["GET", "POST"])
@administrador_requerido
def nuevo():
    conexion = obtener_conexion()
    if conexion is None:
        flash("No se pudo conectar con la base de datos.", "error")
        return redirect(url_for("productos.listar"))
    imagen_guardada = None
    try:
        with conexion.cursor(cursor_factory=RealDictCursor) as cursor:
            categorias = _categorias(cursor)
            if request.method == "GET":
                return render_template("productos/formulario.html", producto=None, categorias=categorias)

            datos = _datos_producto()
            _validar_datos(datos)
            imagen = _leer_imagen()
            if imagen:
                imagen_url, imagen_guardada = imagen
            else:
                imagen_url = None

            id_producto = obtener_siguiente_id(cursor, "producto", "id_producto")
            id_inventario = obtener_siguiente_id(cursor, "inventario", "id_inventario")
            cursor.execute(
                """INSERT INTO comercial.producto
                   (id_producto, id_categoria, nombre, precio, precio_oferta,
                    descripcion, marca, imagen_url, estado)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                (id_producto, datos["id_categoria"], datos["nombre"], datos["precio"],
                 datos["precio_oferta"], datos["descripcion"], datos["marca"],
                 imagen_url, datos["estado"]),
            )
            cursor.execute(
                """INSERT INTO comercial.inventario
                   (id_inventario, id_producto, cantidad, fecha_ingreso, ubicacion)
                   VALUES (%s, %s, %s, CURRENT_DATE, 'Almacén')""",
                (id_inventario, id_producto, datos["stock"]),
            )
        conexion.commit()
    except ValueError as error:
        conexion.rollback()
        if imagen_guardada and os.path.exists(imagen_guardada):
            os.remove(imagen_guardada)
        flash(str(error), "error")
        return render_template(
            "productos/formulario.html", producto=request.form, categorias=categorias
        ), 400
    except Exception:
        conexion.rollback()
        if imagen_guardada and os.path.exists(imagen_guardada):
            os.remove(imagen_guardada)
        raise
    finally:
        conexion.close()

    flash(f"Producto “{datos['nombre']}” creado.", "exito")
    return redirect(url_for("productos.listar"))


@productos_bp.route("/<int:id_producto>/editar", methods=["GET", "POST"])
@administrador_requerido
def editar(id_producto):
    conexion = obtener_conexion()
    if conexion is None:
        flash("No se pudo conectar con la base de datos.", "error")
        return redirect(url_for("productos.listar"))
    imagen_guardada = None
    imagen_anterior = None
    try:
        with conexion.cursor(cursor_factory=RealDictCursor) as cursor:
            categorias = _categorias(cursor)
            cursor.execute(
                """SELECT p.*, COALESCE(i.cantidad, 0) AS stock
                   FROM comercial.producto p
                   LEFT JOIN comercial.inventario i ON i.id_producto = p.id_producto
                   WHERE p.id_producto = %s
                   FOR UPDATE OF p""",
                (id_producto,),
            )
            producto = cursor.fetchone()
            if not producto:
                flash("Producto no encontrado.", "error")
                return redirect(url_for("productos.listar"))
            if request.method == "GET":
                return render_template("productos/formulario.html", producto=producto, categorias=categorias)

            datos = _datos_producto()
            _validar_datos(datos, requiere_stock=False)
            if datos["estado"] == "INACTIVO" and producto["estado"] != "INACTIVO":
                cursor.execute(
                    """SELECT COALESCE(SUM(cantidad), 0) AS reservado
                       FROM comercial.reserva_carrito
                       WHERE id_producto = %s AND vence_en > statement_timestamp()""",
                    (id_producto,),
                )
                if cursor.fetchone()["reservado"]:
                    raise ValueError(
                        "No se puede desactivar el producto mientras esté reservado en un carrito."
                    )
            imagen = _leer_imagen()
            imagen_url = producto["imagen_url"]
            if imagen:
                imagen_url, imagen_guardada = imagen
                imagen_anterior = imagen_url and producto["imagen_url"]

            cursor.execute(
                """UPDATE comercial.producto SET id_categoria = %s, nombre = %s, precio = %s,
                   precio_oferta = %s, descripcion = %s, marca = %s,
                   imagen_url = %s, estado = %s WHERE id_producto = %s""",
                (datos["id_categoria"], datos["nombre"], datos["precio"], datos["precio_oferta"],
                 datos["descripcion"], datos["marca"], imagen_url,
                 datos["estado"], id_producto),
            )
        conexion.commit()
    except ValueError as error:
        conexion.rollback()
        if imagen_guardada and os.path.exists(imagen_guardada):
            os.remove(imagen_guardada)
        flash(str(error), "error")
        return render_template(
            "productos/formulario.html", producto=request.form, categorias=categorias
        ), 400
    except Exception:
        conexion.rollback()
        if imagen_guardada and os.path.exists(imagen_guardada):
            os.remove(imagen_guardada)
        raise
    finally:
        conexion.close()

    if imagen_anterior and imagen_anterior.startswith("/static/uploads/"):
        ruta_anterior = os.path.join(current_app.static_folder, "uploads", os.path.basename(imagen_anterior))
        if os.path.isfile(ruta_anterior):
            os.remove(ruta_anterior)
    flash(f"Producto “{datos['nombre']}” actualizado.", "exito")
    return redirect(url_for("productos.listar"))


@productos_bp.route("/<int:id_producto>/eliminar", methods=["POST"])
@administrador_requerido
def eliminar(id_producto):
    conexion = obtener_conexion()
    if conexion is None:
        flash("No se pudo conectar con la base de datos.", "error")
        return redirect(url_for("productos.listar"))
    try:
        with conexion.cursor(cursor_factory=RealDictCursor) as cursor:
            cursor.execute(
                "SELECT id_producto FROM comercial.producto WHERE id_producto = %s FOR UPDATE",
                (id_producto,),
            )
            if not cursor.fetchone():
                flash("Producto no encontrado.", "error")
                return redirect(url_for("productos.listar"))
            cursor.execute(
                """SELECT COALESCE(SUM(cantidad), 0) AS reservado
                   FROM comercial.reserva_carrito
                   WHERE id_producto = %s AND vence_en > statement_timestamp()""",
                (id_producto,),
            )
            if cursor.fetchone()["reservado"]:
                raise ValueError("No se puede retirar el producto mientras esté reservado en un carrito.")
            cursor.execute(
                "UPDATE comercial.producto SET estado = 'INACTIVO' WHERE id_producto = %s",
                (id_producto,),
            )
        conexion.commit()
    except ValueError as error:
        conexion.rollback()
        flash(str(error), "error")
        return redirect(url_for("productos.listar"))
    except Exception:
        conexion.rollback()
        raise
    finally:
        conexion.close()
    flash("Producto retirado del catálogo.", "exito")
    return redirect(url_for("productos.listar"))


@productos_bp.route("/inventario")
@administrador_requerido
def inventario():
    busqueda = request.args.get("q", "").strip()
    estado = request.args.get("estado", "ACTIVO")
    if estado not in ("ACTIVO", "INACTIVO", "TODOS"):
        estado = "ACTIVO"

    conexion = obtener_conexion()
    if conexion is None:
        flash("No se pudo conectar con la base de datos.", "error")
        return render_template(
            "productos/inventario.html", registros=[], busqueda=busqueda, estado=estado
        ), 503
    try:
        with conexion.cursor(cursor_factory=RealDictCursor) as cursor:
            filtros = []
            parametros = []
            if estado != "TODOS":
                filtros.append("i.estado = %s")
                parametros.append(estado)
            if busqueda:
                filtros.append("(p.nombre ILIKE %s OR COALESCE(i.ubicacion, '') ILIKE %s)")
                parametros.extend((f"%{busqueda}%", f"%{busqueda}%"))
            sql = """SELECT i.id_inventario, i.id_producto, i.cantidad, i.fecha_ingreso,
                            i.ubicacion, i.estado, p.nombre AS producto
                     FROM comercial.inventario i
                     JOIN comercial.producto p ON p.id_producto = i.id_producto"""
            if filtros:
                sql += " WHERE " + " AND ".join(filtros)
            cursor.execute(sql + " ORDER BY p.nombre", tuple(parametros))
            registros = cursor.fetchall()
    finally:
        conexion.close()
    return render_template(
        "productos/inventario.html", registros=registros, busqueda=busqueda, estado=estado
    )


@productos_bp.route("/inventario/nuevo", methods=["GET", "POST"])
@administrador_requerido
def inventario_nuevo():
    conexion = obtener_conexion()
    if conexion is None:
        flash("No se pudo conectar con la base de datos.", "error")
        return redirect(url_for("productos.inventario"))
    try:
        with conexion.cursor(cursor_factory=RealDictCursor) as cursor:
            cursor.execute(
                """SELECT p.id_producto, p.nombre FROM comercial.producto p
                   LEFT JOIN comercial.inventario i ON i.id_producto = p.id_producto
                   WHERE i.id_inventario IS NULL ORDER BY p.nombre"""
            )
            productos_disponibles = cursor.fetchall()
            if request.method == "GET":
                return render_template(
                    "productos/inventario_formulario.html",
                    registro=None,
                    productos=productos_disponibles,
                )

            id_producto = request.form.get("id_producto", type=int)
            cantidad = request.form.get("cantidad", type=int)
            fecha_ingreso = request.form.get("fecha_ingreso") or None
            ubicacion = request.form.get("ubicacion", "").strip()
            if not id_producto or cantidad is None or cantidad < 0:
                raise ValueError("Selecciona un producto e ingresa una cantidad válida.")
            if len(ubicacion) > 150:
                raise ValueError("La ubicación no puede superar 150 caracteres.")

            cursor.execute(
                "SELECT id_producto FROM comercial.producto WHERE id_producto = %s FOR UPDATE",
                (id_producto,),
            )
            if not cursor.fetchone():
                raise ValueError("El producto seleccionado no existe.")
            cursor.execute(
                "SELECT id_inventario FROM comercial.inventario WHERE id_producto = %s",
                (id_producto,),
            )
            if cursor.fetchone():
                raise ValueError("Este producto ya tiene un registro de inventario; edítalo desde el listado.")
            id_inventario = obtener_siguiente_id(cursor, "inventario", "id_inventario")
            cursor.execute(
                """INSERT INTO comercial.inventario
                   (id_inventario, id_producto, cantidad, fecha_ingreso, ubicacion, estado)
                   VALUES (%s, %s, %s, COALESCE(%s::date, CURRENT_DATE), %s, 'ACTIVO')""",
                (id_inventario, id_producto, cantidad, fecha_ingreso, ubicacion or None),
            )
        conexion.commit()
    except ValueError as error:
        conexion.rollback()
        flash(str(error), "error")
        return redirect(url_for("productos.inventario_nuevo"))
    except Exception:
        conexion.rollback()
        raise
    finally:
        conexion.close()
    flash("Registro de inventario creado.", "exito")
    return redirect(url_for("productos.inventario"))


@productos_bp.route("/inventario/<int:id_inventario>/editar", methods=["GET", "POST"])
@administrador_requerido
def inventario_editar(id_inventario):
    conexion = obtener_conexion()
    if conexion is None:
        flash("No se pudo conectar con la base de datos.", "error")
        return redirect(url_for("productos.inventario"))
    try:
        with conexion.cursor(cursor_factory=RealDictCursor) as cursor:
            cursor.execute(
                """SELECT i.id_inventario, i.id_producto, i.cantidad, i.fecha_ingreso,
                          i.ubicacion, i.estado, p.nombre AS producto
                   FROM comercial.inventario i
                   JOIN comercial.producto p ON p.id_producto = i.id_producto
                   WHERE i.id_inventario = %s""",
                (id_inventario,),
            )
            registro = cursor.fetchone()
            if not registro:
                flash("Registro de inventario no encontrado.", "error")
                return redirect(url_for("productos.inventario"))
            if request.method == "GET":
                return render_template(
                    "productos/inventario_formulario.html", registro=registro, productos=[]
                )

            cantidad = request.form.get("cantidad", type=int)
            fecha_ingreso = request.form.get("fecha_ingreso") or None
            ubicacion = request.form.get("ubicacion", "").strip()
            estado = request.form.get("estado", "")
            if cantidad is None or cantidad < 0 or estado not in ("ACTIVO", "INACTIVO"):
                raise ValueError("Ingresa una cantidad y un estado válidos.")
            if estado == "INACTIVO" and cantidad != 0:
                raise ValueError("Un registro inactivo debe tener cantidad cero.")
            if len(ubicacion) > 150:
                raise ValueError("La ubicación no puede superar 150 caracteres.")

            cursor.execute(
                "SELECT id_producto FROM comercial.producto WHERE id_producto = %s FOR UPDATE",
                (registro["id_producto"],),
            )
            cursor.execute(
                "SELECT id_inventario FROM comercial.inventario WHERE id_inventario = %s FOR UPDATE",
                (id_inventario,),
            )
            cursor.execute(
                """SELECT COALESCE(SUM(cantidad), 0) AS reservado
                   FROM comercial.reserva_carrito
                   WHERE id_producto = %s AND vence_en > statement_timestamp()""",
                (registro["id_producto"],),
            )
            reservado = cursor.fetchone()["reservado"]
            if cantidad < reservado or (estado == "INACTIVO" and reservado):
                raise ValueError(
                    f"No puedes reducir el inventario por debajo de las {reservado} "
                    "unidades reservadas en carritos activos."
                )
            cursor.execute(
                """UPDATE comercial.inventario
                   SET cantidad = %s, fecha_ingreso = %s, ubicacion = %s, estado = %s
                   WHERE id_inventario = %s""",
                (cantidad, fecha_ingreso, ubicacion or None, estado, id_inventario),
            )
        conexion.commit()
    except ValueError as error:
        conexion.rollback()
        flash(str(error), "error")
        return redirect(url_for("productos.inventario_editar", id_inventario=id_inventario))
    except Exception:
        conexion.rollback()
        raise
    finally:
        conexion.close()
    flash("Inventario actualizado.", "exito")
    return redirect(url_for("productos.inventario"))


@productos_bp.route("/inventario/<int:id_inventario>/eliminar", methods=["POST"])
@administrador_requerido
def inventario_eliminar(id_inventario):
    conexion = obtener_conexion()
    if conexion is None:
        flash("No se pudo conectar con la base de datos.", "error")
        return redirect(url_for("productos.inventario"))
    try:
        with conexion.cursor(cursor_factory=RealDictCursor) as cursor:
            cursor.execute(
                """SELECT id_producto FROM comercial.inventario
                   WHERE id_inventario = %s""",
                (id_inventario,),
            )
            registro = cursor.fetchone()
            if not registro:
                flash("Registro de inventario no encontrado.", "error")
                return redirect(url_for("productos.inventario"))
            cursor.execute(
                "SELECT id_producto FROM comercial.producto WHERE id_producto = %s FOR UPDATE",
                (registro["id_producto"],),
            )
            cursor.execute(
                """SELECT COALESCE(SUM(cantidad), 0) AS reservado
                   FROM comercial.reserva_carrito
                   WHERE id_producto = %s AND vence_en > statement_timestamp()""",
                (registro["id_producto"],),
            )
            if cursor.fetchone()["reservado"]:
                raise ValueError("No se puede desactivar inventario con unidades reservadas en carritos.")
            cursor.execute(
                """UPDATE comercial.inventario
                   SET estado = 'INACTIVO', cantidad = 0
                   WHERE id_inventario = %s""",
                (id_inventario,),
            )
        conexion.commit()
    except ValueError as error:
        conexion.rollback()
        flash(str(error), "error")
        return redirect(url_for("productos.inventario"))
    except Exception:
        conexion.rollback()
        raise
    finally:
        conexion.close()
    flash("Registro de inventario desactivado; se conservó para mantener el historial.", "exito")
    return redirect(url_for("productos.inventario"))
