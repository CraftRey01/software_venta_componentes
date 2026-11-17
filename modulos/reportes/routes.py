from flask import Blueprint, render_template, request
from db.conexion import obtener_conexion
from modulos.auth.routes import administrador_requerido
from config import Config
import psycopg2.extras

reportes_bp = Blueprint('reportes', __name__)


@reportes_bp.route('/ingresos', methods=['GET', 'POST'])
@administrador_requerido
def ingresos():
    conexion = obtener_conexion()
    cursor = conexion.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    datos = []
    if request.method == 'POST':
        cursor.execute("SELECT categoria, SUM(ingresos) as total_ingresos FROM comercial.vista_ingresos_producto_categoria GROUP BY categoria")
        datos = cursor.fetchall()
    conexion.close()
    return render_template('reportes/ventas.html', datos=datos)


@reportes_bp.route('/rop')
@administrador_requerido
def rop():
    conexion = obtener_conexion()
    cursor = conexion.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cursor.execute(
        """WITH demanda AS (
               SELECT dp.id_producto, SUM(dp.cantidad) AS unidades_vendidas
               FROM comercial.detalle_pedido dp
               JOIN comercial.pedido pe ON pe.id_pedido = dp.id_pedido
               WHERE pe.fecha >= CURRENT_TIMESTAMP - (%s * INTERVAL '1 day')
                 AND pe.estado = ANY(%s)
               GROUP BY dp.id_producto
           )
           SELECT p.nombre, i.cantidad,
                  COALESCE(d.unidades_vendidas, 0) AS demanda_periodo,
                  COALESCE(d.unidades_vendidas, 0)::numeric / %s * %s + %s
                      AS rop_calculado,
                  COALESCE(reservas.cantidad, 0) AS reservado,
                  GREATEST(i.cantidad - COALESCE(reservas.cantidad, 0), 0)
                      AS disponible
           FROM comercial.producto p
           JOIN comercial.inventario i ON p.id_producto = i.id_producto
           LEFT JOIN demanda d ON d.id_producto = p.id_producto
           LEFT JOIN LATERAL (
               SELECT SUM(rc.cantidad) AS cantidad
               FROM comercial.reserva_carrito rc
               WHERE rc.id_producto = p.id_producto
                 AND rc.vence_en > statement_timestamp()
           ) reservas ON TRUE
           WHERE i.estado = 'ACTIVO'
           ORDER BY (GREATEST(i.cantidad - COALESCE(reservas.cantidad, 0), 0)
                     <= COALESCE(d.unidades_vendidas, 0)::numeric / %s * %s + %s) DESC,
                    p.nombre""",
        (
            Config.DEMANDA_PERIODO_DIAS,
            ["CONFIRMADO", "PAGADO", "ENVIADO", "ENTREGADO"],
            Config.DEMANDA_PERIODO_DIAS,
            Config.TIEMPO_ENTREGA_DIAS,
            Config.STOCK_SEGURIDAD_DEFECTO,
            Config.DEMANDA_PERIODO_DIAS,
            Config.TIEMPO_ENTREGA_DIAS,
            Config.STOCK_SEGURIDAD_DEFECTO,
        ),
    )
    alertas = cursor.fetchall()
    conexion.close()
    return render_template(
        'reportes/inventario.html',
        alertas=alertas,
        periodo_demanda=Config.DEMANDA_PERIODO_DIAS,
    )
