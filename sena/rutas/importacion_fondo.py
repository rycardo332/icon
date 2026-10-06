"""Importación de reportes FILPAC en segundo plano.

El estado de cada importación se guarda en memoria, uno por usuario. Sirve con
`runserver` o con un solo proceso. Si algún día se despliega con varios workers
(gunicorn con varios procesos), el estado debería pasar a un modelo en la base de datos.
"""
import logging
import threading
import time

from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import connection

from .importador_filpac import ErrorImportacion, importar_reporte

logger = logging.getLogger(__name__)

_TRABAJOS = {}          # user_id -> dict con el estado
_LOCK = threading.Lock()


def hay_en_curso(user_id):
    t = _TRABAJOS.get(user_id)
    return bool(t and t["estado"] == "procesando")


def iniciar(user, archivos):
    """archivos: lista de (nombre, bytes). Devuelve False si ya hay una importación en curso."""
    with _LOCK:
        if hay_en_curso(user.pk):
            return False
        trabajo = {
            "estado": "procesando",
            "total": len(archivos),
            "hechos": 0,
            "exitosos": 0,
            "archivo_actual": archivos[0][0] if archivos else "",
            "errores": [],
            "resumen": None,
            "inicio": time.time(),
            "fin": None,
            "entregado": False,
        }
        _TRABAJOS[user.pk] = trabajo
    threading.Thread(target=_correr, args=(user, trabajo, archivos), daemon=True).start()
    return True


def _como_dict(r):
    if isinstance(r, dict):
        return r
    return {k: v for k, v in vars(r).items() if not k.startswith("_")}


def _sumar(totales, resumen):
    for k, v in _como_dict(resumen).items():
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            totales[k] = totales.get(k, 0) + v
        else:
            totales.setdefault(k, v)


def _correr(user, t, archivos):
    totales = {}
    try:
        for i, (nombre, datos) in enumerate(archivos):
            t["archivo_actual"] = nombre
            try:
                archivo = SimpleUploadedFile(
                    nombre, datos,
                    content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                )
                _sumar(totales, importar_reporte(archivo, user))
                t["exitosos"] += 1
            except ErrorImportacion as e:
                t["errores"].append(f"{nombre}: {e}")
            except Exception:
                logger.exception("Error inesperado importando %s", nombre)
                t["errores"].append(
                    f"{nombre}: error inesperado al procesar el archivo. Avisa al administrador."
                )
            t["hechos"] = i + 1
        t["resumen"] = totales if t["exitosos"] else None
        t["estado"] = "terminada" if t["exitosos"] else "error"
    except BaseException:
        logger.exception("La importación se interrumpió")
        t["errores"].append("La importación se interrumpió. Avisa al administrador.")
        t["estado"] = "error"
    finally:
        t["fin"] = time.time()
        connection.close()   # cada hilo cierra su propia conexión a la base de datos


def estado(user_id):
    """Estado para la barra de progreso. 'ninguno' si no hay nada que mostrar."""
    t = _TRABAJOS.get(user_id)
    if not t or t["entregado"]:
        return {"estado": "ninguno"}
    fin = t["fin"] or time.time()
    return {
        "estado": t["estado"],
        "total": t["total"],
        "hechos": t["hechos"],
        "archivo_actual": t["archivo_actual"],
        "segundos": int(fin - t["inicio"]),
    }


def entregar_terminado(user_id):
    """Si hay una importación terminada que el usuario aún no ha visto, la entrega una sola vez."""
    t = _TRABAJOS.get(user_id)
    if t and t["estado"] in ("terminada", "error") and not t["entregado"]:
        t["entregado"] = True
        return {"resumen": t["resumen"], "errores": t["errores"], "total": t["total"]}
    return None