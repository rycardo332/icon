"""Genera la narrativa de "1. RUTA PRINCIPAL" a partir de una traza cruda de GPS.

Flujo:
    coords GPS crudas (lat, lon) --> OSRM /match (map-matching + steps)
                                  --> narrativa en español, estilo formato ICON LTDA

Ejemplo de uso:
    from narrativa_ruta_osrm import generar_texto_ruta

    # coords: lista de (lat, lon) tal como vienen del reporte GPS, en orden
    coords = [(5.8280, -73.0340), (5.8266, -73.0331), ...]

    texto = generar_texto_ruta(
        coords,
        origen_texto="las oficinas de la empresa ICON LTDA en la Calle 23 # 18-111 Duitama",
        destino_texto="Cl. 46 #10a-531 a 10a-449",
    )
    print(texto)

Notas importantes:
- Se usa por defecto el servidor público de demo de OSRM (router.project-osrm.org).
  Es gratis pero tiene límites de uso (rate limiting) y NO tiene garantía de
  disponibilidad para producción. Para uso serio/recurrente, monta tu propio
  OSRM (es open source, se levanta con Docker) y cambia OSRM_BASE_URL.
- La calidad del texto depende de qué tan completos estén los nombres de calle
  en OpenStreetMap para esa zona (igual que con los cuerpos de agua). En zonas
  urbanas de Duitama-Sogamoso-Nobsa suele estar razonablemente bien mapeado;
  en tramos muy rurales puede salir "continúa por la vía" en vez de un nombre.
- El resultado es más "seco"/estilo Google Maps que el texto humano. Ajusta las
  plantillas de _frase_paso() a tu gusto para acercarlo más al estilo actual.
"""
import logging
import time

import requests

logger = logging.getLogger(__name__)

OSRM_BASE_URL = "https://router.project-osrm.org"
_TIMEOUT_HTTP = (5, 30)
_MAX_PUNTOS_OSRM = 95  # el servidor público de demo limita ~100 coords por request

MODIFICADOR_ES = {
    "uturn": "dar la vuelta en U",
    "sharp right": "girar bruscamente a la derecha",
    "right": "girar a la derecha",
    "slight right": "girar levemente a la derecha",
    "straight": "continuar derecho",
    "slight left": "girar levemente a la izquierda",
    "left": "girar a la izquierda",
    "sharp left": "girar bruscamente a la izquierda",
}

_ORDINALES = {1: "1.ª", 2: "2.ª", 3: "3.ª", 4: "4.ª", 5: "5.ª", 6: "6.ª"}


class ErrorOSRM(Exception):
    pass


def _adelgazar_por_indice(coords, maximo):
    """Reduce la traza a `maximo` puntos como mucho, tomando uno cada N.
    Simple y suficiente aquí; si ya usas `_adelgazar` (por distancia) en
    mapa_viajes.py, es mejor pasar la traza ya reducida por esa función."""
    if len(coords) <= maximo:
        return coords
    paso = len(coords) / maximo
    indices = sorted({round(i * paso) for i in range(maximo)})
    indices[-1] = len(coords) - 1  # asegurar que el último punto quede incluido
    return [coords[i] for i in indices if i < len(coords)]


def _consultar_match(coords, reintentos=2):
    """Llama a OSRM /match con la traza (lat, lon) y devuelve el JSON.
    `coords` debe venir ya reducida a <= _MAX_PUNTOS_OSRM puntos."""
    perfil_lonlat = ";".join(f"{lon:.6f},{lat:.6f}" for lat, lon in coords)
    url = f"{OSRM_BASE_URL}/match/v1/driving/{perfil_lonlat}"
    params = {
        "steps": "true",
        "geometries": "geojson",
        "overview": "full",
        "annotations": "false",
    }
    ultimo_error = None
    for intento in range(1, reintentos + 1):
        try:
            resp = requests.get(url, params=params, timeout=_TIMEOUT_HTTP)
            resp.raise_for_status()
            data = resp.json()
            if data.get("code") != "Ok":
                raise ErrorOSRM(f"OSRM respondió code={data.get('code')}: {data.get('message', '')}")
            return data
        except (requests.RequestException, ValueError, ErrorOSRM) as e:
            ultimo_error = e
            logger.warning("OSRM /match falló (intento %s/%s): %s", intento, reintentos, e)
            time.sleep(1.5)
    raise ErrorOSRM(f"OSRM /match no respondió tras {reintentos} intentos: {ultimo_error}")


def _pasos_de_matching(data):
    """Aplana los steps de todos los matchings/legs, en orden."""
    pasos = []
    for matching in data.get("matchings", []):
        for leg in matching.get("legs", []):
            pasos.extend(leg.get("steps", []))
    return pasos


def _frase_paso(paso, es_primero, es_ultimo, origen_texto, destino_texto):
    """Convierte un step de OSRM en una frase en español."""
    maniobra = paso.get("maneuver", {})
    tipo = maniobra.get("type", "")
    modificador = maniobra.get("modifier", "")
    nombre_via = (paso.get("name") or "").strip()
    exit_num = maniobra.get("exit")

    via_o_generico = nombre_via if nombre_via else "la vía"

    if es_primero or tipo == "depart":
        base = f"Se inicia la ruta en {origen_texto}" if origen_texto else "Se inicia la ruta"
        if nombre_via:
            return f"{base}, tomando {via_o_generico}."
        return f"{base}."

    if tipo in ("roundabout", "rotary"):
        # el nombre de la vía de salida suele venir en el siguiente step
        # (tipo "exit roundabout"/"exit rotary"); aquí solo anunciamos la rotonda
        return "Se aproxima a una rotonda."

    if tipo in ("exit roundabout", "exit rotary"):
        ordinal = _ORDINALES.get(exit_num, f"{exit_num}.ª" if exit_num else "siguiente")
        return f"En la rotonda, toma la {ordinal} salida{f' hacia {via_o_generico}' if nombre_via else ''}."

    if tipo == "turn":
        accion = MODIFICADOR_ES.get(modificador, "girar")
        return f"{accion.capitalize()}{f' hacia {via_o_generico}' if nombre_via else ''}."

    if tipo in ("new name", "continue"):
        return f"Continúa por {via_o_generico}."

    if tipo in ("merge", "on ramp", "off ramp", "fork", "end of road"):
        accion = MODIFICADOR_ES.get(modificador, "continuar")
        return f"{accion.capitalize()}{f' hacia {via_o_generico}' if nombre_via else ''}."

    if es_ultimo or tipo == "arrive":
        lado = MODIFICADOR_ES.get(modificador, "")
        destino = destino_texto or via_o_generico
        if lado:
            return f"El destino está a {lado.replace('girar a ', 'la ').replace('continuar derecho', 'la vista')} en {destino}."
        return f"Ha llegado al destino: {destino}."

    # tipo desconocido / notification / use lane: se omite para no ensuciar el texto
    return ""


def narrativa_ruta(data, origen_texto=None, destino_texto=None):
    """A partir del JSON crudo de OSRM /match, arma el párrafo narrativo."""
    pasos = _pasos_de_matching(data)
    if not pasos:
        return "No fue posible construir la narrativa: OSRM no devolvió pasos de ruta."

    frases = []
    total = len(pasos)
    for i, paso in enumerate(pasos):
        frase = _frase_paso(
            paso,
            es_primero=(i == 0),
            es_ultimo=(i == total - 1),
            origen_texto=origen_texto,
            destino_texto=destino_texto,
        )
        if frase:
            frases.append(frase)

    return " ".join(frases)


def generar_texto_ruta(coords_gps, origen_texto=None, destino_texto=None):
    """Punto de entrada principal.

    coords_gps: lista de tuplas (lat, lon), en el orden en que vienen del GPS.
    origen_texto / destino_texto: texto libre para reemplazar el inicio/fin
        genérico (ej. la dirección de las oficinas, o la dirección de destino
        tal como la escriben a mano en el formato).

    Devuelve el párrafo de texto listo para el campo "1. RUTA PRINCIPAL".
    Lanza ErrorOSRM si OSRM no responde tras los reintentos.
    """
    if not coords_gps or len(coords_gps) < 2:
        raise ValueError("Se necesitan al menos 2 puntos de GPS para generar la ruta.")

    coords_reducidas = _adelgazar_por_indice(coords_gps, _MAX_PUNTOS_OSRM)
    data = _consultar_match(coords_reducidas)
    return narrativa_ruta(data, origen_texto=origen_texto, destino_texto=destino_texto)


if __name__ == "__main__":
    # Prueba rápida con un par de puntos de ejemplo en Duitama-Sogamoso.
    # Reemplaza esto por una traza real exportada de tu reporte GPS.
    coords_ejemplo = [
        (5.8280, -73.0340),
        (5.8210, -73.0250),
        (5.8100, -73.0150),
        (5.7990, -73.0080),
        (5.7850, -72.9980),
        (5.7720, -72.9350),
    ]
    try:
        texto = generar_texto_ruta(
            coords_ejemplo,
            origen_texto="las oficinas de la empresa ICON LTDA en la Calle 23 # 18-111 Duitama",
            destino_texto="Cl. 46 #10a-531 a 10a-449",
        )
        print(texto)
    except ErrorOSRM as e:
        print(f"OSRM no respondió: {e}")