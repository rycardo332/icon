"""Geocodificación inversa (coordenada -> dirección legible) con Nominatim.

- Nominatim es el buscador de direcciones de OpenStreetMap. Regla de uso: máximo
  1 solicitud por segundo, y un User-Agent que identifique la aplicación.
- Cada respuesta se guarda en MEDIA_ROOT/cache_geocodificacion/ (un JSON por
  coordenada, redondeada a 4 decimales ≈ 11 m). La primera vez cuesta ~1 s por
  punto; las siguientes son instantáneas.
- Si Nominatim no responde, se devuelve None / la coordenada cruda: nunca lanza
  excepciones, así que no rompe la generación del Excel. Tras varios fallos
  seguidos se deja de intentar por un rato, para no esperar timeouts en cada punto.
"""
import json
import threading
import time
from pathlib import Path

import requests
from django.conf import settings

NOMINATIM_URL = "https://nominatim.openstreetmap.org/reverse"
HEADERS = {"User-Agent": "ICON-LTDA-reportes/1.0 (uso interno)", "Accept-Language": "es"}

PAUSA_ENTRE_SOLICITUDES_S = 1.1   
TIMEOUT_S = 8
FALLOS_PARA_PAUSAR = 3            
PAUSA_TRAS_FALLOS_S = 120

_candado = threading.Lock()
_ultima_solicitud = 0.0
_fallos_seguidos = 0
_pausado_hasta = 0.0


def _archivo_cache(lat, lon):
    carpeta = Path(settings.MEDIA_ROOT) / "cache_geocodificacion"
    carpeta.mkdir(parents=True, exist_ok=True)
    return carpeta / f"{lat:.4f}_{lon:.4f}.json"


def _resumir(data):
    """Del JSON de Nominatim saca calle, barrio/vereda, municipio y un texto corto."""
    a = data.get("address") or {}
    calle = a.get("road") or a.get("pedestrian") or a.get("path") or a.get("residential") or ""
    if calle and a.get("house_number"):
        calle = f"{calle} #{a['house_number']}"
    barrio = (a.get("neighbourhood") or a.get("suburb") or a.get("quarter")
              or a.get("hamlet") or "")
    municipio = (a.get("city") or a.get("town") or a.get("village")
                 or a.get("municipality") or a.get("county") or "")

    partes = [p for p in (calle, barrio, municipio) if p]
    if not partes:
        nombre = data.get("display_name") or ""
        partes = [p.strip() for p in nombre.split(",")[:2] if p.strip()]
    return {"calle": calle, "barrio": barrio, "municipio": municipio, "texto": ", ".join(partes)}


def _limpiar(d):
    """Quita el 'Cabecera Municipal ' que Nominatim antepone en Colombia
    ('Cabecera Municipal Duitama' -> 'Duitama'). Se aplica al leer, así también
    limpia lo que ya estaba guardado en la caché."""
    if not d:
        return d
    return {k: (v.replace("Cabecera Municipal ", "") if isinstance(v, str) else v)
            for k, v in d.items()}


def direccion_de_coordenada(lat, lon):
    """Devuelve {'calle','barrio','municipio','texto'} o None si no se pudo obtener."""
    global _ultima_solicitud, _fallos_seguidos, _pausado_hasta

    archivo = _archivo_cache(lat, lon)
    if archivo.exists():
        try:
            return _limpiar(json.loads(archivo.read_text(encoding="utf-8"))) or None
        except (OSError, ValueError):
            pass  # cache dañada: se vuelve a pedir

    with _candado:
        ahora = time.monotonic()
        if ahora < _pausado_hasta:
            return None
        espera = PAUSA_ENTRE_SOLICITUDES_S - (ahora - _ultima_solicitud)
        if espera > 0:
            time.sleep(espera)
        try:
            resp = requests.get(
                NOMINATIM_URL,
                params={"format": "jsonv2", "lat": f"{lat:.6f}", "lon": f"{lon:.6f}",
                        "zoom": 18, "addressdetails": 1},
                headers=HEADERS, timeout=TIMEOUT_S,
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception:
            _ultima_solicitud = time.monotonic()
            _fallos_seguidos += 1
            if _fallos_seguidos >= FALLOS_PARA_PAUSAR:
                _pausado_hasta = time.monotonic() + PAUSA_TRAS_FALLOS_S
                _fallos_seguidos = 0
            return None
        _ultima_solicitud = time.monotonic()
        _fallos_seguidos = 0

    resumen = {} if "error" in data else _resumir(data)
    try:
        archivo.write_text(json.dumps(resumen, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass
    return _limpiar(resumen) or None


def texto_ubicacion(lat, lon):
    """Dirección legible; si no hay, la coordenada cruda ('5.71320, -72.93010')."""
    d = direccion_de_coordenada(lat, lon)
    if d and d.get("texto"):
        return d["texto"]
    return f"{lat:.5f}, {lon:.5f}"