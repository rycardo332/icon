"""Entorno de la ruta (Overpass): centros poblados, cuerpos de agua y puntos de apoyo.

- Centros poblados: nodos con la etiqueta `place` (ciudad, municipio, vereda, caserío).
- Cuerpos de agua:
    * líneas `waterway` (río, quebrada, canal) que pasan a <= radio_agua_m de la ruta.
    * áreas `natural=water` con nombre (lago, embalse, laguna), que no tienen `waterway`
      y por eso antes no aparecían (ej. Lago de Tota).
  Se calcula el punto más cercano a la ruta y, alrededor de ese punto, se busca el
  puente o la vía para armar la "ubicación" (como en la plantilla).
- Apoyo de emergencias: bomberos, policía, Cruz Roja y hospitales, con teléfono,
  ciudad, dirección y equipos.

Cambios frente a la versión anterior:
- Consulta a Overpass con reintentos, espera entre intentos y caché en memoria.
- Detecta respuestas "200 pero con error" (timeout de Overpass con 0 elementos), que era
  lo que dejaba la tabla 7 vacía sin avisar.
- Lugares y aguas van en consultas separadas: si una falla no se pierde la otra.
- Los errores quedan en el log y en ERRORES_ENTORNO (se reinicia en cada llamada).
- FIX: el "enfriamiento" tras un fallo de red/servidor ahora es por tipo de consulta
  (lugares/aguas/lagos/apoyo/superficie/ubicacion_agua), no global. Antes, si
  _buscar_lugares fallaba por un 429 de Overpass, se activaba un enfriamiento de
  _ENFRIAMIENTO_S compartido por TODO el módulo, y la siguiente consulta de aguas
  fallaba de inmediato sin ni siquiera intentar preguntarle a Overpass.
- FIX: el caché en memoria ya NO trata "200 OK con 0 elementos y sin remark" como un
  resultado confiable durante 1 hora. Una respuesta vacía se guarda con un TTL mucho más
  corto (_CACHE_TTL_VACIO_S) y se deja un WARNING explícito en el log para que sea
  visible que Overpass respondió "bien" pero sin datos.
- NUEVO: _buscar_lagos busca lagos, embalses y lagunas (natural=water) y los suma a la
  lista de cuerpos de agua.

Reutiliza los servidores, utilidades y excepciones de mapa_viajes.py.
"""
import hashlib
import logging
import time
import unicodedata

import requests

from .mapa_viajes import (
    OVERPASS_ENDPOINTS,
    ErrorOverpass,
    _adelgazar,
    distancia_metros,
)

logger = logging.getLogger(__name__)

TIPOS_LUGAR = {
    "city": "Ciudad",
    "town": "Municipio / centro poblado",
    "village": "Centro poblado / vereda",
    "hamlet": "Caserío / vereda",
}
TIPOS_AGUA = {
    "river": "RÍO",
    "stream": "QUEBRADA",
    "canal": "CANAL",
    "lake": "LAGO",
    "reservoir": "EMBALSE",
    "pond": "LAGUNA",
}

TEXTO_SIN_AGUA = "Sin cuerpos de agua cerca de la ruta"
TEXTO_PENDIENTE = "Verificar"

MAX_PUNTOS_POLILINEA = 300  # para que la consulta `around` no sea gigante


ERRORES_ENTORNO = []

# --------------------------------------------------------------------------------------
# Teléfonos verificados a mano (sacados de la plantilla original). OSM casi nunca los trae.
# Clave: (entidad, ciudad) en minúsculas y sin tildes.
# Entidades: "bomberos", "policia", "cruz roja", "hospital regional".
# Complétalos/corrígelos aquí; lo que no esté sale como "Verificar".
# --------------------------------------------------------------------------------------
TELEFONOS_LOCALES = {
    ("bomberos", "duitama"): "119 - 760 2949",
    ("policia", "duitama"): "123 - 156 - 350 5561017",
    ("cruz roja", "duitama"): "763 0754 - 312 397 5269",
    ("hospital regional", "duitama"): "763 2323",
    ("cruz roja", "sogamoso"): "312 421 3394",
    ("hospital regional", "sogamoso"): "770 2201",
}

ORDEN_APOYO = {"fire_station": 0, "police": 1, "cruz_roja": 2, "hospital": 3}
ENTIDAD_APOYO = {
    "fire_station": "BOMBEROS",
    "police": "POLICÍA NACIONAL",
    "cruz_roja": "CRUZ ROJA",
    "hospital": "HOSPITAL",
}
EQUIPOS_APOYO = {
    "fire_station": "Equipo de bomberos",
    "police": "Patrulla",
    "cruz_roja": "Paramédico - Ambulancia",
    "hospital": "Equipo médico",
}
CLAVE_TELEFONO = {
    "fire_station": "bomberos",
    "police": "policia",
    "cruz_roja": "cruz roja",
    "hospital": "hospital",
}


# --------------------------------------------------------------------------------------
# Utilidades
# --------------------------------------------------------------------------------------
def _norm(texto):
    """Minúsculas y sin tildes, para comparar nombres."""
    texto = unicodedata.normalize("NFD", texto or "")
    return "".join(c for c in texto if unicodedata.category(c) != "Mn").lower().strip()


_CACHE = {}
_CACHE_TTL_S = 3600
_CACHE_TTL_VACIO_S = 60


_TIMEOUT_HTTP = (5, 70)
_PRESUPUESTO_S = 150
_ENFRIAMIENTO_S = 180


_ULTIMO_FALLO = {}


def _familia(etiqueta):
    """'aguas_tramo_3' -> 'aguas' (todos los tramos comparten el mismo cooldown).
    'lugares', 'lagos', 'apoyo', 'superficie', 'ubicacion_agua' quedan igual (no tienen tramos)."""
    return etiqueta.split("_tramo_")[0]


def _consultar_overpass(query, reintentos=1, etiqueta="general"):
    """POST a Overpass probando cada servidor `reintentos` veces, con tope de tiempo.

    `etiqueta` identifica el tipo de consulta (lugares, aguas, lagos, ubicacion_agua,
    apoyo, superficie). El enfriamiento tras un fallo de red/servidor se guarda por
    etiqueta, así el fallo de un tipo de consulta no bloquea a los demás.

    - Reintenta con espera corta en 429/502/503/504 y errores de red.
    - Trata como error una respuesta 200 con 'remark' (timeout / out of memory)
      y cero elementos.
    - Cachea en memoria: 1 hora si trajo elementos, solo 1 minuto si vino vacía sin
      remark (para no perpetuar un vacío transitorio durante una hora entera).
    - Se rinde a los _PRESUPUESTO_S segundos. Si el fallo fue de red/servidor, las
      siguientes consultas CON LA MISMA ETIQUETA fallan al instante durante
      _ENFRIAMIENTO_S (en vez de repetir todos los intentos una y otra vez).
    - Si todo falla lanza ErrorOverpass con el detalle de cada intento.
    """
    clave = hashlib.md5(query.encode("utf-8")).hexdigest()
    guardado = _CACHE.get(clave)
    if guardado:
        momento_guardado, ttl_guardado, data_guardada = guardado
        if time.time() - momento_guardado < ttl_guardado:
            return data_guardada

    familia = _familia(etiqueta)
    momento_fallo, detalle_fallo = _ULTIMO_FALLO.get(familia, (0.0, ""))
    if time.time() - momento_fallo < _ENFRIAMIENTO_S:
        raise ErrorOverpass(f"Overpass no responde (falló hace poco): {detalle_fallo}")

    headers = {"User-Agent": "ICON-LTDA-reportes/1.0 (uso interno)", "Accept": "application/json"}
    errores = []
    fallo_de_red = False
    inicio = time.time()
    agotado = False
    for url in OVERPASS_ENDPOINTS:
        for intento in range(1, reintentos + 1):
            if time.time() - inicio > _PRESUPUESTO_S:
                agotado = True
                errores.append(f"tiempo máximo ({_PRESUPUESTO_S} s) agotado")
                break
            try:
                resp = requests.post(url, data={"data": query}, headers=headers, timeout=_TIMEOUT_HTTP)
                if resp.status_code in (429, 502, 503, 504):
                    fallo_de_red = True
                    errores.append(f"{url}: HTTP {resp.status_code} (intento {intento})")
                    logger.warning("Overpass %s -> HTTP %s (intento %s)", url, resp.status_code, intento)
                    time.sleep(1)
                    continue
                resp.raise_for_status()
                data = resp.json()
                remark = data.get("remark", "")
                elementos = data.get("elements") or []
                if remark and not elementos:
                    errores.append(f"{url}: {remark[:120]} (intento {intento})")
                    logger.warning("Overpass %s -> remark: %s", url, remark[:200])
                    time.sleep(1)
                    continue
                _ULTIMO_FALLO.pop(familia, None)
                if elementos:
                    ttl = _CACHE_TTL_S
                else:
                    # 200 OK, sin remark, pero 0 elementos: puede ser genuino (no hay
                    # nada que buscar) o un vacío transitorio del servidor. No lo
                    # tratamos como error (no hay nada que reintentar aquí y ahora),
                    # pero tampoco lo dejamos "envenenando" el caché por una hora.
                    ttl = _CACHE_TTL_VACIO_S
                    logger.warning(
                        "Overpass %s -> 200 OK sin remark pero 0 elementos (etiqueta=%s). "
                        "Se guarda en caché solo %s s, no %s s, por si es transitorio.",
                        url, etiqueta, _CACHE_TTL_VACIO_S, _CACHE_TTL_S,
                    )
                _CACHE[clave] = (time.time(), ttl, data)
                return data
            except requests.HTTPError as e:
                # 4xx (consulta mal armada): es un problema de esta consulta, no del servidor
                errores.append(f"{url}: {e} (intento {intento})")
                logger.warning("Overpass %s -> %s (intento %s)", url, e, intento)
            except (requests.RequestException, ValueError) as e:
                fallo_de_red = True
                errores.append(f"{url}: {e} (intento {intento})")
                logger.warning("Overpass %s -> %s (intento %s)", url, e, intento)
                time.sleep(1)
        if agotado:
            break
    detalle = " | ".join(errores) or "sin servidores configurados"
    if fallo_de_red or agotado:
        _ULTIMO_FALLO[familia] = (time.time(), detalle[:200])
    logger.error("Overpass falló en todos los servidores (%s): %s", etiqueta, detalle)
    raise ErrorOverpass(detalle)


def _largo_m(ruta):
    return sum(distancia_metros(a[0], a[1], b[0], b[1]) for a, b in zip(ruta, ruta[1:]))


def _bbox(ruta, margen):
    lats, lons = [p[0] for p in ruta], [p[1] for p in ruta]
    return f"{min(lats) - margen},{min(lons) - margen},{max(lats) + margen},{max(lons) + margen}"


# --------------------------------------------------------------------------------------
# Lugares y cuerpos de agua
# --------------------------------------------------------------------------------------
def _buscar_lugares(ruta, radio_lugares_m):
    query = (
        "[out:json][timeout:60];"
        f'node["place"~"^(city|town|village|hamlet)$"]["name"]({_bbox(ruta, 0.03)});out;'
    )
    data = _consultar_overpass(query, etiqueta="lugares")
    lugares = []
    for el in data.get("elements", []):
        tags = el.get("tags", {})
        nombre = tags.get("name")
        if not nombre or el["type"] != "node" or "place" not in tags:
            continue
        lat, lon = el["lat"], el["lon"]
        dist = min(distancia_metros(lat, lon, plat, plon) for plat, plon in ruta)
        if dist <= radio_lugares_m:
            tipo = tags["place"]
            lugares.append({
                "nombre": nombre, "tipo": tipo, "etiqueta": TIPOS_LUGAR.get(tipo, "Lugar"),
                "lat": lat, "lon": lon, "distancia_a_ruta_m": round(dist),
            })
    lugares.sort(key=lambda p: p["distancia_a_ruta_m"])
    return lugares


def _dividir_en_tramos(ruta, largo_tramo_m=5000):
    """Parte la ruta en tramos de ~largo_tramo_m metros cada uno, con un poco de
    traslape entre tramos consecutivos para no perder ríos justo en la frontera."""
    if not ruta:
        return []
    tramos = []
    tramo_actual = [ruta[0]]
    largo_acumulado = 0.0
    for a, b in zip(ruta, ruta[1:]):
        largo_acumulado += distancia_metros(a[0], a[1], b[0], b[1])
        tramo_actual.append(b)
        if largo_acumulado >= largo_tramo_m:
            tramos.append(tramo_actual)
            tramo_actual = [b]
            largo_acumulado = 0.0
    if len(tramo_actual) > 1:
        tramos.append(tramo_actual)
    return tramos


def _buscar_lagos(ruta, radio_agua_m):
    """Lagos, embalses y lagunas (natural=water), que no tienen 'waterway' y por eso
    _buscar_aguas no los encuentra. Se buscan sobre toda la ruta de una sola vez
    (no por tramos), porque son pocos y grandes, no hace falta dividir la consulta."""
    margen = max(0.02, (radio_agua_m / 100000) * 2)
    query = (
        "[out:json][timeout:60];"
        f'(way["natural"="water"]["name"]({_bbox(ruta, margen)});'
        f'relation["natural"="water"]["name"]({_bbox(ruta, margen)}););'
        "out geom;"
    )
    try:
        data = _consultar_overpass(query, etiqueta="lagos")
    except ErrorOverpass as e:
        ERRORES_ENTORNO.append(f"lagos/embalses: {e}")
        return []

    lagos_por_nombre = {}
    for el in data.get("elements", []):
        tags = el.get("tags", {})
        nombre = tags.get("name")
        if not nombre:
            continue
        if el["type"] == "way":
            geom = [(g["lat"], g["lon"]) for g in el.get("geometry", [])]
        else:  # relation: junta la geometría de todos sus miembros
            geom = [(g["lat"], g["lon"]) for m in el.get("members", []) for g in m.get("geometry", [])]
        if not geom:
            continue
        geom = _adelgazar(geom, paso_m=60)
        d, punto = min(
            ((distancia_metros(g[0], g[1], r[0], r[1]), g) for g in geom for r in ruta),
            key=lambda t: t[0],
        )
        if d > radio_agua_m:
            continue
        tipo = tags.get("water", "lake")
        candidato = {
            "nombre": nombre, "tipo": tipo, "etiqueta": TIPOS_AGUA.get(tipo, "LAGO"),
            "lat": punto[0], "lon": punto[1], "distancia_a_ruta_m": round(d),
            "ubicacion": "",
        }
        previo = lagos_por_nombre.get(nombre)
        if previo is None or candidato["distancia_a_ruta_m"] < previo["distancia_a_ruta_m"]:
            lagos_por_nombre[nombre] = candidato
    return list(lagos_por_nombre.values())


def _buscar_aguas(ruta, radio_agua_m):
    tramos = _dividir_en_tramos(ruta, largo_tramo_m=10000)
    margen = max(0.02, (radio_agua_m / 100000) * 2)

    aguas_por_nombre = {}
    fallos_tramo = []
    for i, tramo in enumerate(tramos):
        query = (
            "[out:json][timeout:60];"
            f'way["waterway"~"^(river|stream|canal)$"]["name"]({_bbox(tramo, margen)});'
            "out geom;"
        )
        try:
            data = _consultar_overpass(query, etiqueta=f"aguas_tramo_{i}")
        except ErrorOverpass as e:
            fallos_tramo.append(f"tramo {i}: {e}")
            continue

        for el in data.get("elements", []):
            tags = el.get("tags", {})
            nombre = tags.get("name")
            if not nombre or el["type"] != "way" or "waterway" not in tags:
                continue
            geom = [(g["lat"], g["lon"]) for g in el.get("geometry", [])]
            if not geom:
                continue
            geom = _adelgazar(geom, paso_m=60)
            d, punto = min(
                ((distancia_metros(g[0], g[1], r[0], r[1]), g) for g in geom for r in ruta),
                key=lambda t: t[0],
            )
            if d > radio_agua_m:
                continue
            tipo = tags["waterway"]
            candidato = {
                "nombre": nombre, "tipo": tipo, "etiqueta": TIPOS_AGUA.get(tipo, "CUERPO DE AGUA"),
                "lat": punto[0], "lon": punto[1], "distancia_a_ruta_m": round(d),
                "ubicacion": "",
            }
            previo = aguas_por_nombre.get(nombre)
            if previo is None or candidato["distancia_a_ruta_m"] < previo["distancia_a_ruta_m"]:
                aguas_por_nombre[nombre] = candidato

    if fallos_tramo:
        ERRORES_ENTORNO.append("cuerpos de agua (parcial): " + " | ".join(fallos_tramo))
    if not aguas_por_nombre and fallos_tramo:
        # Ningún tramo trajo nada Y todos fallaron: ahí sí es un fallo real, no parcial.
        raise ErrorOverpass(" | ".join(fallos_tramo))

    # Lagos, embalses y lagunas (natural=water): consulta aparte, si falla no rompe lo demás.
    for lago in _buscar_lagos(ruta, radio_agua_m):
        nombre = lago["nombre"]
        previo = aguas_por_nombre.get(nombre)
        if previo is None or lago["distancia_a_ruta_m"] < previo["distancia_a_ruta_m"]:
            aguas_por_nombre[nombre] = lago

    aguas = sorted(aguas_por_nombre.values(), key=lambda p: p["distancia_a_ruta_m"])
    _poner_ubicaciones(aguas)
    return aguas


def _poner_ubicaciones(aguas):
    """Rellena a['ubicacion'] con el puente o la vía donde la ruta cruza/bordea el agua.
    Si esta segunda consulta falla, se deja un texto con las coordenadas (no rompe nada)."""
    if not aguas:
        return
    partes = []
    for a in aguas:
        lat, lon = a["lat"], a["lon"]
        partes.append(f'way["highway"]["bridge"](around:80,{lat:.5f},{lon:.5f});')
        partes.append(f'way["highway"]["name"](around:40,{lat:.5f},{lon:.5f});')
    query = "[out:json][timeout:40];(" + "".join(partes) + ");out geom;"
    try:
        ways = _consultar_overpass(query, etiqueta="ubicacion_agua").get("elements", [])
    except ErrorOverpass as e:
        ERRORES_ENTORNO.append(f"ubicación de cuerpos de agua: {e}")
        ways = []
    for a in aguas:
        a["ubicacion"] = _ubicacion_de_punto(a, ways)


def _ubicacion_de_punto(agua, ways):
    lat, lon = agua["lat"], agua["lon"]
    mejor_puente = None   # (prioridad, distancia, texto)
    mejor_via = None      # (distancia, nombre)
    for w in ways:
        geom = [(g["lat"], g["lon"]) for g in w.get("geometry", [])]
        if not geom:
            continue
        tags = w.get("tags", {})
        d = min(distancia_metros(lat, lon, g[0], g[1]) for g in geom)
        via = tags.get("name", "")
        if tags.get("bridge") and tags["bridge"] != "no" and d <= 80:
            nombre_puente = tags.get("bridge:name")
            if nombre_puente:
                texto = nombre_puente if _norm(nombre_puente).startswith("puente") else f"Puente {nombre_puente}"
                prioridad = 0
            elif via:
                texto, prioridad = f"Puente en {via}", 1
            else:
                continue
            if mejor_puente is None or (prioridad, d) < mejor_puente[:2]:
                mejor_puente = (prioridad, d, texto)
        if via and d <= 40 and (mejor_via is None or d < mejor_via[0]):
            mejor_via = (d, via)
    if mejor_puente:
        return mejor_puente[2]
    if mejor_via:
        return mejor_via[1]
    return f"Cruce a {agua['distancia_a_ruta_m']} m de la ruta ({lat:.5f}, {lon:.5f})"


def buscar_entorno_ruta(coords_ruta, radio_lugares_m=1500, radio_agua_m=150):
    """Devuelve (lugares, aguas), cada una lista de dicts con
    nombre, tipo, etiqueta, lat, lon, distancia_a_ruta_m (ordenadas por cercanía).
    Las aguas traen además 'ubicacion' (puente o vía del cruce).

    Si una de las dos consultas falla NO se pierde la otra: el error queda en el log y en
    ERRORES_ENTORNO. Si fallan las dos se lanza ErrorOverpass."""
    ERRORES_ENTORNO.clear()
    ruta = _adelgazar(coords_ruta, paso_m=100)
    if not ruta:
        return [], []

    lugares, aguas, fallos = [], [], 0
    try:
        lugares = _buscar_lugares(ruta, radio_lugares_m)
    except ErrorOverpass as e:
        fallos += 1
        ERRORES_ENTORNO.append(f"centros poblados: {e}")
    try:
        aguas = _buscar_aguas(ruta, radio_agua_m)
    except ErrorOverpass as e:
        fallos += 1
        ERRORES_ENTORNO.append(f"cuerpos de agua: {e}")

    if fallos == 2:
        raise ErrorOverpass(" || ".join(ERRORES_ENTORNO))
    return lugares, aguas


# --------------------------------------------------------------------------------------
# Puntos de apoyo (bomberos, policía, Cruz Roja, hospitales)
# --------------------------------------------------------------------------------------
def _clasificar_apoyo(tags):
    nombre = _norm(tags.get("name", ""))
    if "cruz roja" in nombre or "red cross" in nombre:
        return "cruz_roja"
    amenity = tags.get("amenity", "")
    return amenity if amenity in ("fire_station", "police", "hospital") else None


def _ciudad_de(lat, lon, tags, lugares):
    """Ciudad/municipio: el place city|town más cercano (si se pasan `lugares`,
    hasta 8 km); si no, addr:city de OSM."""
    if lugares:
        candidatos = [p for p in lugares if p.get("tipo") in ("city", "town")]
        if candidatos:
            p = min(candidatos, key=lambda p: distancia_metros(lat, lon, p["lat"], p["lon"]))
            if distancia_metros(lat, lon, p["lat"], p["lon"]) <= 8000:
                return p["nombre"]
    return tags.get("addr:city", "")


def _telefono_osm(tags):
    for k in ("phone", "contact:phone", "contact:mobile", "mobile"):
        if tags.get(k):
            return tags[k].replace(";", " - ").strip()
    return ""


def telefono_para(tel_osm, tipo, nombre, ciudad):
    """(telefono, fuente). Prioridad: OSM -> TELEFONOS_LOCALES -> 'Verificar'."""
    if tel_osm:
        return tel_osm, "osm"
    clave = CLAVE_TELEFONO.get(tipo, tipo)
    if tipo == "hospital" and "regional" in _norm(nombre):
        clave = "hospital regional"
    tel = TELEFONOS_LOCALES.get((clave, _norm(ciudad)))
    if tel:
        return tel, "tabla local"
    return TEXTO_PENDIENTE, "pendiente"


def _quitar_duplicados(items):
    """Quita el mismo punto repetido: mismo nombre a <1.5 km, o (bomberos/policía/Cruz Roja
    o alguno sin nombre) a <250 m. Se queda con el que tenga más datos."""
    def datos(i):
        return (bool(i["_tel_osm"]), i["nombre"] != "(sin nombre)", bool(i["direccion_osm"]))

    unicos = []
    for it in sorted(items, key=datos, reverse=True):
        duplicado = False
        for k in unicos:
            if k["tipo"] != it["tipo"]:
                continue
            d = distancia_metros(it["lat"], it["lon"], k["lat"], k["lon"])
            mismo_nombre = it["nombre"] != "(sin nombre)" and _norm(it["nombre"]) == _norm(k["nombre"])
            sin_nombre = "(sin nombre)" in (it["nombre"], k["nombre"])
            if (mismo_nombre and d < 1500) or ((sin_nombre or it["tipo"] != "hospital") and d < 250):
                duplicado = True
                break
        if not duplicado:
            unicos.append(it)
    return unicos


def buscar_apoyo_emergencias(coords_ruta, radio_m=5000, lugares=None):
    ruta = _adelgazar(coords_ruta, paso_m=100)
    if not ruta:
        return []
    bbox = _bbox(ruta, 0.05)
    query = (
        "[out:json][timeout:60];("
        f'nwr["amenity"~"^(fire_station|police|hospital)$"]({bbox});'
        f'nwr["name"~"Cruz Roja",i]({bbox});'
        ");out center tags;"
    )
    data = _consultar_overpass(query, etiqueta="apoyo")  # si falla, sube ErrorOverpass: que se vea

    crudos = []
    for el in data.get("elements", []):
        if el["type"] == "node":
            lat, lon = el["lat"], el["lon"]
        else:
            centro = el.get("center")
            if not centro:
                continue
            lat, lon = centro["lat"], centro["lon"]
        tags = el.get("tags", {})
        tipo = _clasificar_apoyo(tags)
        if not tipo:
            continue
        dist = min(distancia_metros(lat, lon, plat, plon) for plat, plon in ruta)
        if dist > radio_m:
            continue
        crudos.append({
            "lat": lat, "lon": lon,
            "nombre": tags.get("name", "(sin nombre)"),
            "tipo": tipo,
            "distancia_a_ruta_m": round(dist),
            "direccion_osm": " ".join(x for x in (tags.get("addr:street"), tags.get("addr:housenumber")) if x),
            "_tel_osm": _telefono_osm(tags),
            "_tags": tags,
        })

    resultados = []
    for it in _quitar_duplicados(crudos):
        ciudad = _ciudad_de(it["lat"], it["lon"], it["_tags"], lugares)
        tel, fuente = telefono_para(it["_tel_osm"], it["tipo"], it["nombre"], ciudad)
        direccion = it["direccion_osm"]
        if direccion and ciudad:
            direccion = f"{direccion}, {ciudad}"
        entidad = ENTIDAD_APOYO[it["tipo"]]
        if it["tipo"] == "hospital" and it["nombre"] != "(sin nombre)":
            entidad = it["nombre"].upper()
        resultados.append({
            "lat": it["lat"], "lon": it["lon"], "nombre": it["nombre"], "tipo": it["tipo"],
            "distancia_a_ruta_m": it["distancia_a_ruta_m"],
            "telefono": tel, "telefono_fuente": fuente,
            "direccion_osm": it["direccion_osm"],
            "direccion": direccion or TEXTO_PENDIENTE,
            "ciudad": ciudad,
            "entidad": entidad,
            "equipos": EQUIPOS_APOYO[it["tipo"]],
        })
    resultados.sort(key=lambda r: (_norm(r["ciudad"]), ORDEN_APOYO[r["tipo"]], r["distancia_a_ruta_m"]))
    return resultados


# --------------------------------------------------------------------------------------
# Superficie y ancho de vía (OSM) — pre-rellena tipo_superficie en la Ruta
# --------------------------------------------------------------------------------------

# Mapeo de valores OSM surface -> TipoSuperficie del modelo
_SURFACE_OSM = {
    "asphalt": "pavimentada",
    "paved": "pavimentada",
    "concrete": "pavimentada",
    "cobblestone": "pavimentada",
    "unpaved": "trocha",
    "dirt": "trocha",
    "gravel": "trocha",
    "ground": "trocha",
    "grass": "trocha",
    "mud": "trocha",
    "sand": "trocha",
    "compacted": "trocha",
}


def buscar_superficie_via(coords_ruta, radio_m=30):
    """Consulta los tags surface, lanes y width de las vías que pasan cerca de la ruta.

    Devuelve un dict con las claves de TipoSuperficie que se pueden marcar
    automáticamente:
      - 'pavimentada' o 'trocha' (según surface de OSM)
      - 'angosta' (lanes == 1 o width <= 4)
      - 'amplia'  (lanes >= 2 o width >= 6)

    Si Overpass no responde devuelve {} sin lanzar excepción.
    Uso en generador_excel.py:
        auto = buscar_superficie_via(coords)
        if not ruta_obj.tipo_superficie:   # solo si el usuario no marcó nada
            ruta_obj.tipo_superficie = list(auto.keys())
    """
    # Se limita el número de puntos igual que en _buscar_aguas, para que la consulta
    # `around` no sea gigante en rutas largas.
    ruta = _adelgazar(coords_ruta, paso_m=max(150, _largo_m(coords_ruta) / MAX_PUNTOS_POLILINEA))
    if not ruta:
        return {}

    # Consultamos vías con highway cerca de la polilínea de la ruta
    polilinea = ",".join(f"{lat:.5f},{lon:.5f}" for lat, lon in ruta)
    query = (
        "[out:json][timeout:40];"
        f'way["highway"](around:{radio_m},{polilinea});'
        "out tags;"
    )
    try:
        data = _consultar_overpass(query, etiqueta="superficie")
    except ErrorOverpass as e:
        logger.warning("buscar_superficie_via: Overpass no respondió (%s)", e)
        return {}

    # Contamos votos por cada valor
    votos_surface = {}      # "pavimentada" | "trocha"  -> conteo
    votos_ancho = {}        # "angosta" | "amplia"      -> conteo

    for el in data.get("elements", []):
        tags = el.get("tags", {})

        # --- Superficie ---
        surface = tags.get("surface", "").lower()
        valor_superficie = _SURFACE_OSM.get(surface)
        if valor_superficie:
            votos_surface[valor_superficie] = votos_surface.get(valor_superficie, 0) + 1

        # --- Ancho / carriles ---
        try:
            lanes = int(tags.get("lanes", 0))
        except (ValueError, TypeError):
            lanes = 0
        try:
            width = float(tags.get("width", 0))
        except (ValueError, TypeError):
            width = 0.0

        if lanes == 1 or (0 < width <= 4):
            votos_ancho["angosta"] = votos_ancho.get("angosta", 0) + 1
        elif lanes >= 2 or width >= 6:
            votos_ancho["amplia"] = votos_ancho.get("amplia", 0) + 1

    resultado = {}

    # Superficie: gana la que tenga más votos
    if votos_surface:
        resultado[max(votos_surface, key=votos_surface.get)] = True

    # Ancho: gana el que tenga más votos (angosta/amplia son mutuamente excluyentes)
    if votos_ancho:
        resultado[max(votos_ancho, key=votos_ancho.get)] = True

    return resultado  # ej. {"pavimentada": True, "angosta": True}