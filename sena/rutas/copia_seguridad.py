import io
import json
import logging
import os
import tempfile
import zipfile
from datetime import datetime

from django.apps import apps
from django.conf import settings
from django.contrib import messages
from django.core.management import call_command
from django.http import HttpResponse
from django.shortcuts import redirect, render
from django.views.decorators.http import require_POST

from .usuarios import solo_administradores

logger = logging.getLogger(__name__)

MAX_ZIP_MB = 200
MAX_JSON_MB = 100


class ErrorBackup(Exception):
    """Error de validación con un mensaje claro para el usuario."""


LEEME = """COPIA DE SEGURIDAD - ICON LTDA

Contenido:
- datos.json : todos los datos de la base (usuarios con contraseñas cifradas incluidos)
- media/     : archivos subidos (si existen)

Como restaurar:
- Desde la pagina: menu Copias de seguridad > Restaurar copia, y subir este mismo zip.
- Desde la terminal (base nueva):
    python manage.py migrate
    python manage.py loaddata datos.json
  y copiar la carpeta media/ a MEDIA_ROOT.

Este archivo contiene informacion sensible. Guardalo en un lugar privado.
"""


# ---------------------------------------------------------------- DESCARGAR
@solo_administradores
@require_POST
def descargar_backup(request):
    salida = io.StringIO()
    call_command(
        'dumpdata',
        exclude=['contenttypes', 'auth.permission', 'sessions', 'admin.logentry'],
        natural_foreign=True,
        natural_primary=True,
        indent=2,
        stdout=salida,
    )

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as zf:
        zf.writestr('datos.json', salida.getvalue())
        zf.writestr('LEEME.txt', LEEME)

        media_root = getattr(settings, 'MEDIA_ROOT', None)
        if media_root and os.path.isdir(media_root):
            for carpeta, _, archivos in os.walk(media_root):
                for nombre in archivos:
                    ruta_completa = os.path.join(carpeta, nombre)
                    relativa = os.path.relpath(ruta_completa, media_root)
                    zf.write(ruta_completa, os.path.join('media', relativa))

    nombre_zip = f"backup_icon_{datetime.now():%Y-%m-%d_%H%M}.zip"
    respuesta = HttpResponse(buffer.getvalue(), content_type='application/zip')
    respuesta['Content-Disposition'] = f'attachment; filename="{nombre_zip}"'
    return respuesta


# ---------------------------------------------------------------- RESTAURAR
def _buscar_datos(zf):
    """Busca datos.json en la raíz del zip o dentro de una carpeta.
    Devuelve (nombre_en_zip, prefijo_de_carpeta) o (None, '')."""
    for nombre in zf.namelist():
        if nombre.endswith('/') or nombre.startswith('__MACOSX'):
            continue
        if os.path.basename(nombre).lower() == 'datos.json':
            prefijo = nombre[: -len('datos.json')]
            return nombre, prefijo
    return None, ''


def _validar_y_leer(archivo):
    """Valida el zip. Devuelve (zipfile, lista_de_objetos, nombre_json, prefijo)."""
    if archivo.size > MAX_ZIP_MB * 1024 * 1024:
        raise ErrorBackup(
            f"El archivo es demasiado grande (el máximo es {MAX_ZIP_MB} MB). "
            "Verifica que sea la copia que descargaste desde este sistema."
        )
    if not zipfile.is_zipfile(archivo):
        raise ErrorBackup(
            "Ese archivo no es un .zip. Sube la copia que descargaste con el botón "
            "«Descargar copia de seguridad»."
        )

    archivo.seek(0)
    zf = zipfile.ZipFile(archivo)

    nombre_json, prefijo = _buscar_datos(zf)
    if not nombre_json:
        raise ErrorBackup(
            "Este .zip no parece una copia de seguridad del sistema (no trae el archivo de datos). "
            "Usa el .zip tal como se descargó, sin abrirlo ni volver a comprimirlo."
        )
    if zf.getinfo(nombre_json).file_size > MAX_JSON_MB * 1024 * 1024:
        raise ErrorBackup("El archivo de datos de la copia es demasiado grande para restaurarlo desde la página.")

    try:
        datos = json.loads(zf.read(nombre_json).decode('utf-8'))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ErrorBackup(
            "La copia está dañada y no se puede leer. Descarga una copia nueva e inténtalo de nuevo."
        )

    if not isinstance(datos, list) or len(datos) == 0:
        raise ErrorBackup(
            "La copia está vacía: no trae ningún dato para restaurar. "
            "Sube una copia que se haya descargado con información."
        )

    for obj in datos:
        if not isinstance(obj, dict) or 'model' not in obj or 'fields' not in obj:
            raise ErrorBackup(
                "El archivo de datos no tiene el formato esperado, así que no parece una copia de este sistema."
            )
        try:
            apps.get_model(obj['model'])
        except (LookupError, ValueError):
            raise ErrorBackup(
                "Esta copia pertenece a otro proyecto o a una versión distinta del sistema, "
                "por eso no se puede restaurar aquí."
            )
    return zf, datos, nombre_json, prefijo


def _restaurar(archivo):
    zf, datos, nombre_json, prefijo = _validar_y_leer(archivo)

    # Carga en la base. loaddata es atómico: si falla, no queda nada a medias.
    with tempfile.TemporaryDirectory() as tmp:
        ruta_json = os.path.join(tmp, 'datos.json')
        with open(ruta_json, 'wb') as f:
            f.write(zf.read(nombre_json))
        call_command('loaddata', ruta_json, verbosity=0)

    # Archivos subidos (solo si la base cargó bien)
    archivos_media = 0
    media_root = getattr(settings, 'MEDIA_ROOT', None)
    if media_root:
        base = os.path.abspath(media_root)
        inicio_media = prefijo + 'media/'
        for nombre in zf.namelist():
            if not nombre.startswith(inicio_media) or nombre.endswith('/'):
                continue
            destino = os.path.abspath(os.path.join(base, nombre[len(inicio_media):]))
            if not destino.startswith(base + os.sep):  # evita rutas maliciosas (../)
                continue
            os.makedirs(os.path.dirname(destino), exist_ok=True)
            with open(destino, 'wb') as f:
                f.write(zf.read(nombre))
            archivos_media += 1

    return len(datos), archivos_media


@solo_administradores
def restaurar_backup(request):
    if request.method == 'POST':
        archivo = request.FILES.get('archivo')
        if not archivo:
            messages.error(
                request,
                "Primero elige el archivo de la copia: arrastra el .zip a la zona punteada o haz clic para buscarlo.",
            )
        elif not request.POST.get('confirmo'):
            messages.warning(request, "Marca la casilla de confirmación para poder restaurar la copia.")
        else:
            try:
                registros, media = _restaurar(archivo)
                messages.success(
                    request,
                    f"¡Copia restaurada con éxito! Se cargaron {registros} registros "
                    f"y {media} archivos subidos.",
                )
            except ErrorBackup as e:
                messages.error(request, str(e))
            except Exception:
                logger.exception("Error al restaurar la copia de seguridad")
                messages.error(
                    request,
                    "No se pudo restaurar la copia. Tus datos actuales no se modificaron. "
                    "Descarga una copia nueva e inténtalo otra vez; si sigue fallando, avisa al administrador del sistema.",
                )
        return redirect('restaurar_backup')

    return render(request, 'gps/restaurar_backup.html')