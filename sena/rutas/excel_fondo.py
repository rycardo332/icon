"""Generación de Excel en segundo plano (tarjeta de ruta, que tarda unos 5 minutos).

El estado se guarda en memoria, por usuario. Sirve con `runserver` o un solo proceso;
con varios workers habría que pasarlo a un modelo en la base de datos.
"""
import os
import threading
import time

from django.db import connection

_TRABAJOS = {}          # (user_id, clave) -> dict con el estado
_LOCK = threading.Lock()
VIGENCIA_SEG = 3600     # un trabajo terminado se sigue mostrando hasta 1 hora


def _nuevo(titulo, nombre_descarga):
    return {
        "titulo": titulo,
        "nombre_descarga": nombre_descarga,
        "estado": "procesando",
        "inicio": time.time(),
        "fin": None,
        "ruta_archivo": None,
        "error": "",
        "visto": False,
    }


def iniciar(user_id, clave, titulo, nombre_descarga, funcion):
    """Arranca la generación en un hilo. Si ya hay una igual en curso, no duplica."""
    with _LOCK:
        t = _TRABAJOS.get((user_id, clave))
        if t and t["estado"] == "procesando":
            return t
        t = _nuevo(titulo, nombre_descarga)
        _TRABAJOS[(user_id, clave)] = t
    threading.Thread(target=_correr, args=(t, funcion), daemon=True).start()
    return t


def registrar_listo(user_id, clave, titulo, nombre_descarga, ruta_archivo):
    """Para un Excel que ya estaba generado y vigente: queda listo de inmediato."""
    with _LOCK:
        t = _nuevo(titulo, nombre_descarga)
        t.update(estado="listo", fin=time.time(), ruta_archivo=str(ruta_archivo))
        _TRABAJOS[(user_id, clave)] = t
    return t


def _correr(t, funcion):
    try:
        t["ruta_archivo"] = str(funcion())
        t["estado"] = "listo"
    except BaseException as e:
        t["estado"] = "error"
        t["error"] = str(e) or e.__class__.__name__
    finally:
        t["fin"] = time.time()
        connection.close()   # cada hilo cierra su propia conexión a la base de datos


def listar(user_id):
    """Trabajos del usuario que todavía hay que mostrar (en curso, listos sin descargar o con error)."""
    ahora = time.time()
    salida = []
    for (uid, clave), t in list(_TRABAJOS.items()):
        if uid != user_id or t["visto"]:
            continue
        if t["fin"] and ahora - t["fin"] > VIGENCIA_SEG:
            continue
        if t["estado"] == "listo" and not (t["ruta_archivo"] and os.path.exists(t["ruta_archivo"])):
            continue
        salida.append({
            "clave": clave,
            "titulo": t["titulo"],
            "estado": t["estado"],
            "segundos": int((t["fin"] or ahora) - t["inicio"]),
            "error": t["error"],
        })
    return salida


def obtener(user_id, clave):
    return _TRABAJOS.get((user_id, clave))


def marcar_visto(user_id, clave):
    t = _TRABAJOS.get((user_id, clave))
    if t:
        t["visto"] = True