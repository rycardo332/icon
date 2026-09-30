"""Instrucciones de ruta paso a paso (estilo "Gira a la izquierda con dirección a Cl. 22. A 400 metros.").

Para cada Viaje del GPS pide a OSRM las maniobras (steps=true) entre el punto de salida y el de
llegada, las guarda en Viaje.pasos_osrm (así solo se consulta una vez por viaje) y las convierte
en texto en español para el campo "Ruta a seguir" del Word (SIG-FT-77).

Uso:
    from .instrucciones_ruta import lineas_de_ruta
    parrafos = lineas_de_ruta(viajes)      # [] si no se pudo (sin coordenadas u OSRM caído)

Los nombres de calle vienen de OpenStreetMap; en zonas rurales pueden ser menos completos que
los de Google Maps, así que conviene revisar el texto la primera vez.
"""
import logging
import time

import requests

try:
    from .mapa_viajes import OSRM_URL, ErrorOSRM, parsear_coordenadas
except ImportError:  # pruebas fuera del paquete de Django
    from mapa_viajes import OSRM_URL, ErrorOSRM, parsear_coordenadas

log = logging.getLogger(__name__)

GIRO = {
    "left": "a la izquierda",
    "right": "a la derecha",
    "slight left": "levemente a la izquierda",
    "slight right": "levemente a la derecha",
    "sharp left": "bruscamente a la izquierda",
    "sharp right": "bruscamente a la derecha",
}
LADO = {
    "left": "a la izquierda", "slight left": "a la izquierda", "sharp left": "a la izquierda",
    "right": "a la derecha", "slight right": "a la derecha", "sharp right": "a la derecha",
}
PUNTOS_CARDINALES = ["norte", "noreste", "este", "sureste", "sur", "suroeste", "oeste", "noroeste"]


# --------------------------------------------------------------------------------------
# OSRM
# --------------------------------------------------------------------------------------
def pedir_pasos_osrm(origen, destino, intentos=2):
    """Maniobras entre dos puntos (lat, lon). Devuelve una lista de pasos compactos:
    {t: tipo, m: modificador, n: nombre, r: ref, d: metros, s: segundos, x: salida, b: rumbo}."""
    url = (f"{OSRM_URL}/{origen[1]},{origen[0]};{destino[1]},{destino[0]}"
           f"?overview=false&steps=true&geometries=geojson")
    ultimo_error = None
    for intento in range(intentos):
        try:
            resp = requests.get(url, timeout=25)
            resp.raise_for_status()
            data = resp.json()
            if data.get("code") != "Ok":
                raise ErrorOSRM(data.get("message", "OSRM no pudo calcular la ruta"))
            pasos = []
            for leg in data["routes"][0]["legs"]:
                for st in leg["steps"]:
                    man = st.get("maneuver", {})
                    pasos.append({
                        "t": man.get("type", ""),
                        "m": man.get("modifier", ""),
                        "n": st.get("name", ""),
                        "r": st.get("ref", ""),
                        "d": round(st.get("distance", 0)),
                        "s": round(st.get("duration", 0)),
                        "x": man.get("exit"),
                        "b": man.get("bearing_after"),
                    })
            return pasos
        except Exception as e:
            ultimo_error = e
            time.sleep(1.0 * (intento + 1))
    raise ErrorOSRM(str(ultimo_error))


def obtener_pasos(viaje):
    """Pasos del viaje: los guardados, o los pide a OSRM y los guarda. None si no se puede."""
    if viaje.pasos_osrm:
        return viaje.pasos_osrm
    origen = parsear_coordenadas(viaje.coordenadas_inicio)
    destino = parsear_coordenadas(viaje.coordenadas_fin)
    if origen is None or destino is None:
        return None
    try:
        pasos = pedir_pasos_osrm(origen, destino)
    except ErrorOSRM as e:
        log.warning("OSRM no dio pasos para el viaje %s: %s", getattr(viaje, "pk", "?"), e)
        return None
    viaje.pasos_osrm = pasos
    viaje.save(update_fields=["pasos_osrm"])
    time.sleep(0.3)  # cortesía con el servidor público de OSRM
    return pasos


# --------------------------------------------------------------------------------------
# Texto en español
# --------------------------------------------------------------------------------------
def _dist(metros):
    if metros < 1000:
        m = int(round(metros / 10.0) * 10) if metros >= 100 else int(round(metros))
        m = max(m, 1)
        return f"{m} metro" if m == 1 else f"{m} metros"
    return f"{metros / 1000:.1f} km"


def _dur(segundos):
    if segundos < 60:
        return f"{int(round(segundos))} s"
    minutos = int(round(segundos / 60))
    if minutos < 60:
        return f"{minutos} min"
    return f"{minutos // 60} h {minutos % 60} min"


def _cardinal(rumbo):
    return PUNTOS_CARDINALES[int((rumbo + 22.5) // 45) % 8]


def _nombre(p):
    """Nombre de la vía; si solo tiene código (ej. "55"), se dice "la vía 55"."""
    if p.get("n"):
        return p["n"]
    return f"la vía {p['r']}" if p.get("r") else ""


def _instruccion(p):
    t, m, n = p.get("t", ""), p.get("m", ""), _nombre(p)
    por = f" por {n}" if n else ""
    hacia = f" con dirección a {n}" if n else ""

    if t == "depart":
        if p.get("b") is not None:
            return f"Dirígete al {_cardinal(p['b'])}{por}"
        return f"Dirígete hacia {n}" if n else "Inicia el recorrido"
    if t == "arrive":
        lado = LADO.get(m)
        return f"El destino está {lado}" if lado else "Llegaste a tu destino"
    if t in ("turn", "end of road"):
        if m == "uturn":
            return f"Haz un retorno{' en ' + n if n else ''}"
        if m in ("straight", ""):
            return f"Continúa{por}"
        prefijo = "Al final de la vía, gira" if t == "end of road" else "Gira"
        return f"{prefijo} {GIRO.get(m, '')}{hacia}"
    if t == "merge":
        return f"Incorpórate{' a ' + n if n else ''}"
    if t == "on ramp":
        return f"Toma la rampa{' hacia ' + n if n else ''}"
    if t == "off ramp":
        return f"Toma la salida{' hacia ' + n if n else ''}"
    if t == "fork":
        lado = LADO.get(m)
        return f"En la bifurcación, mantente {lado}{por}" if lado else f"Continúa{por}"
    if t in ("roundabout", "rotary"):
        if p.get("x"):
            return f"En la rotonda, toma la {p['x']}.ª salida{' en dirección a ' + n if n else ''}"
        return "Entra a la rotonda"
    if t == "roundabout turn":
        return f"En la rotonda, gira {GIRO.get(m, '')}{hacia}".replace("gira  ", "gira ")
    if t in ("exit roundabout", "exit rotary"):
        return f"Sal de la rotonda{' hacia ' + n if n else ''}"
    return f"Continúa{por}"


def texto_viaje(viaje, pasos):
    """Un párrafo con todas las instrucciones del viaje."""
    origen = (viaje.direccion_origen or "").strip()
    destino = (viaje.direccion_destino or "").strip()
    partes = [f"Se inicia la ruta en {origen}." if origen else "Se inicia la ruta."]
    total_m = total_s = 0
    for p in pasos:
        total_m += p.get("d", 0)
        total_s += p.get("s", 0)
        frase = _instruccion(p)
        if p.get("t") == "arrive":
            partes.append(f"{frase}.")
        else:
            partes.append(f"{frase}. A {_dist(p.get('d', 0))}.")
    cierre = f"A {_dur(total_s)} ({_dist(total_m)})."
    if destino:
        cierre += f" {destino}."
    partes.append(cierre)
    return " ".join(partes)


def _hora(dt):
    try:
        from django.utils import timezone
        if timezone.is_aware(dt):
            dt = timezone.localtime(dt)
    except Exception:
        pass
    return f"{dt.hour % 12 or 12}:{dt.minute:02d} {'am' if dt.hour < 12 else 'pm'}"


def lineas_de_ruta(viajes):
    """Párrafos para la celda "Ruta a seguir": uno por viaje del día (con encabezado si hay
    más de uno). Devuelve [] si ningún viaje tiene pasos."""
    viajes = list(viajes or [])
    parrafos = []
    for n, v in enumerate(viajes, 1):
        pasos = obtener_pasos(v)
        if not pasos:
            continue
        texto = texto_viaje(v, pasos)
        if len(viajes) > 1:
            texto = f"Viaje {n} ({_hora(v.fecha_hora_inicio)} - {_hora(v.fecha_hora_fin)}): {texto}"
        parrafos.append(texto)
    return parrafos