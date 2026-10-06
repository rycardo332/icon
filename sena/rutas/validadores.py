"""Validación de los Excel (.xlsx) y de las imágenes que suben los usuarios."""
import zipfile
from pathlib import Path

from django.core.exceptions import ValidationError

MAX_ARCHIVOS = 10
MAX_BYTES_POR_ARCHIVO = 25 * 1024 * 1024       # 25 MB por archivo
MAX_BYTES_DESCOMPRIMIDO = 300 * 1024 * 1024    # contra "bombas zip" (archivos que se inflan al abrirlos)

TAMANO_MAX_IMAGEN_MB = 5


class ArchivoInvalido(Exception):
    pass


def validar_tamano_imagen(archivo):
    """Validador para ImageField: rechaza imágenes muy pesadas."""
    limite = TAMANO_MAX_IMAGEN_MB * 1024 * 1024
    if archivo.size > limite:
        raise ValidationError(
            f"La imagen no puede pesar más de {TAMANO_MAX_IMAGEN_MB} MB."
        )


def validar_xlsx(archivo):
    """Revisa extensión, tamaño y contenido. Devuelve el nombre limpio (sin carpetas)."""
    nombre = Path(archivo.name).name
    if not nombre.lower().endswith(".xlsx"):
        raise ArchivoInvalido(f"«{nombre}»: solo se aceptan archivos .xlsx.")
    if archivo.size == 0:
        raise ArchivoInvalido(f"«{nombre}» está vacío.")
    if archivo.size > MAX_BYTES_POR_ARCHIVO:
        mb = MAX_BYTES_POR_ARCHIVO // (1024 * 1024)
        raise ArchivoInvalido(f"«{nombre}» pesa demasiado (máximo {mb} MB).")

    archivo.seek(0)
    if archivo.read(4) != b"PK\x03\x04":   # un .xlsx real es un zip
        raise ArchivoInvalido(f"«{nombre}» no es un Excel válido.")
    archivo.seek(0)

    try:
        with zipfile.ZipFile(archivo) as z:
            nombres = {i.filename for i in z.infolist()}
            if "[Content_Types].xml" not in nombres or not any(n.startswith("xl/") for n in nombres):
                raise ArchivoInvalido(f"«{nombre}» no parece un Excel de verdad.")
            if sum(i.file_size for i in z.infolist()) > MAX_BYTES_DESCOMPRIMIDO:
                raise ArchivoInvalido(f"«{nombre}» es demasiado grande al abrirlo.")
    except zipfile.BadZipFile:
        raise ArchivoInvalido(f"«{nombre}» está dañado o no es un Excel válido.")
    finally:
        archivo.seek(0)
    return nombre