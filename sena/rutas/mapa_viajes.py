"""Mapa de viajes FILPAC (Folium + OSRM + Overpass).

Toma los Viaje ya importados a la base de datos y arma un mapa con:
  - un color y una capa por viaje (ruta por carretera calculada con OSRM);
  - las maniobras menores (RegistroGPS sin Viaje) en una sola capa, apagada por defecto;
  - las curvas cerradas mas fuertes de la ruta (agrupadas en el mapa para no saturarlo);
  - opcionalmente, puntos de apoyo y riesgo cercanos (hospitales, bomberos, policía,
    Cruz Roja, colegios, etc.) via Overpass.

La ruta de OSRM se guarda en Viaje.geometria_osrm la primera vez, asi que solo la primera
apertura de un viaje depende del servicio publico. Las respuestas de Overpass se guardan
en MEDIA_ROOT/cache_overpass/ (30 dias), asi que repetir la misma ruta es instantaneo y
no se golpea el servidor publico.

Este modulo no importa modelos de Django: recibe objetos con los atributos de Viaje y
RegistroGPS. Asi se puede probar por separado.
"""
import hashlib
import html
import json
import logging
import math
import re
import time
import unicodedata
from pathlib import Path

import requests

log = logging.getLogger(__name__)

OSRM_URL = "https://router.project-osrm.org/route/v1/driving"
OVERPASS_ENDPOINTS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
]
OVERPASS_DIAS_CACHE = 30


TOLERANCIA_DISTANCIA_OSRM_PCT = 25


VENTANA_CURVA_M = 40
GIRO_MINIMO_GRADOS = 60       
GIRO_MAXIMO_GRADOS = 150     
SEPARACION_MINIMA_ENTRE_CURVAS_M = 400   
DISTANCIA_MIN_DEDUP_CURVAS_GEO_M = 40
MAX_CURVAS_MAPA = 25         


RADIO_POR_TIPO = {
    "hospital": 3000,
    "fire_station": 3000,
    "police": 3000,
    "red_cross": 3000,
    "school": 500,
    "university": 500,
    "college": 500,
    "fuel": 500,
    "toll_booth": 500,
    "speed_camera": 500,
    "roundabout": 100,
}

PALETA_VIAJES = [
    "#e6194b", "#3cb44b", "#4363d8", "#f58231", "#911eb4",
    "#008080", "#f032e6", "#9a6324", "#800000", "#000075",
]


class ErrorOSRM(Exception):
    pass


class ErrorOverpass(Exception):
    pass



def parsear_coordenadas(texto):
    """'5.7132 -72.9301' -> (5.7132, -72.9301). None si no es valido."""
    m = re.match(r"\s*(-?\d+\.\d+)\s+(-?\d+\.\d+)\s*$", str(texto or ""))
    return (float(m.group(1)), float(m.group(2))) if m else None


def distancia_metros(lat1, lon1, lat2, lon2):
    R = 6371000
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return 2 * R * math.asin(min(1, math.sqrt(a)))


def rumbo(lat1, lon1, lat2, lon2):
    phi1, phi2 = map(math.radians, (lat1, lat2))
    dlambda = math.radians(lon2 - lon1)
    x = math.sin(dlambda) * math.cos(phi2)
    y = math.cos(phi1) * math.sin(phi2) - math.sin(phi1) * math.cos(phi2) * math.cos(dlambda)
    return math.degrees(math.atan2(x, y)) % 360


def _sin_acentos(texto):
    t = unicodedata.normalize("NFD", str(texto or ""))
    return "".join(c for c in t if unicodedata.category(c) != "Mn").lower().strip()


def _local(dt):
    """Pasa un datetime con zona horaria a la hora local del proyecto (America/Bogota)."""
    if dt is not None and getattr(dt, "tzinfo", None) is not None:
        from django.utils import timezone
        return timezone.localtime(dt)
    return dt


def _hora(dt):
    dt = _local(dt)
    return dt.strftime("%H:%M") if dt else "-"


def _fecha_hora(dt):
    dt = _local(dt)
    return dt.strftime("%Y-%m-%d %H:%M") if dt else "-"


def _num(valor, decimales=2, sufijo=""):
    return "-" if valor is None else f"{float(valor):.{decimales}f}{sufijo}"




def pedir_ruta_osrm(origen, destino, intentos=2):
    """Camino por carretera entre dos puntos. Devuelve (geometria, distancia_km)."""
    url = (f"{OSRM_URL}/{origen[1]},{origen[0]};{destino[1]},{destino[0]}"
           f"?overview=full&geometries=geojson")
    ultimo_error = None
    for intento in range(intentos):
        try:
            resp = requests.get(url, timeout=25)
            resp.raise_for_status()
            data = resp.json()
            if data.get("code") != "Ok":
                raise ErrorOSRM(data.get("message", "OSRM no pudo calcular la ruta"))
            ruta = data["routes"][0]
            geometria = [(lat, lon) for lon, lat in ruta["geometry"]["coordinates"]]
            return geometria, ruta["distance"] / 1000
        except Exception as e:
            ultimo_error = e
            time.sleep(1.0 * (intento + 1))
    raise ErrorOSRM(str(ultimo_error))


def resolver_ruta(viaje, origen, destino):
    """(geometria, distancia_osrm_km, es_ruta_real). Usa la ruta guardada si existe;
    si no, la pide a OSRM y la guarda. Si OSRM falla, devuelve linea recta y NO guarda
    nada, para reintentar la proxima vez."""
    if viaje.geometria_osrm and viaje.distancia_osrm_km is not None:
        return [tuple(p) for p in viaje.geometria_osrm], float(viaje.distancia_osrm_km), True

    try:
        geometria, km = pedir_ruta_osrm(origen, destino)
    except ErrorOSRM:
        recta_km = distancia_metros(origen[0], origen[1], destino[0], destino[1]) / 1000
        return [origen, destino], recta_km, False

    viaje.geometria_osrm = [[round(lat, 5), round(lon, 5)] for lat, lon in geometria]
    viaje.distancia_osrm_km = round(km, 2)
    viaje.save(update_fields=["geometria_osrm", "distancia_osrm_km"])
    time.sleep(0.3)  
    return geometria, km, True


def preparar_viaje(viaje, indice):
    """Junta un Viaje con su ruta lista para dibujar. None si no tiene coordenadas de inicio."""
    origen = parsear_coordenadas(viaje.coordenadas_inicio)
    if origen is None:
        return None
    destino = parsear_coordenadas(viaje.coordenadas_fin)

    info = {
        "viaje": viaje,
        "indice": indice,
        "color": PALETA_VIAJES[(indice - 1) % len(PALETA_VIAJES)],
        "origen": origen,
        "destino": destino,
        "geometria": [origen],
        "distancia_osrm_km": None,
        "ruta_real": False,
    }
    if destino is not None:
        geometria, km, real = resolver_ruta(viaje, origen, destino)
        info.update(geometria=geometria, distancia_osrm_km=km, ruta_real=real)
    return info




def detectar_tramos_criticos(ruta, ventana_m=VENTANA_CURVA_M,
                             angulo_minimo=GIRO_MINIMO_GRADOS,
                             angulo_maximo=GIRO_MAXIMO_GRADOS,
                             separacion_minima_m=SEPARACION_MINIMA_ENTRE_CURVAS_M):
    """Recorre una ruta y mide cuanto cambia el rumbo entre 'ventana_m' metros antes y
    despues de cada punto. Devuelve lista de (distancia_acumulada_m, giro_grados, (lat, lon)).
    Curvas a menos de 'separacion_minima_m' una de otra se juntan en la mas cerrada."""
    n = len(ruta)
    if n < 3:
        return []

    dist_acum = [0.0]
    for i in range(1, n):
        dist_acum.append(dist_acum[-1] + distancia_metros(ruta[i - 1][0], ruta[i - 1][1],
                                                          ruta[i][0], ruta[i][1]))

    def punto_a_distancia(idx, offset):
        objetivo = dist_acum[idx] + offset
        j = idx
        if offset >= 0:
            while j < n - 1 and dist_acum[j] < objetivo:
                j += 1
        else:
            while j > 0 and dist_acum[j] > objetivo:
                j -= 1
        return j

    candidatos = []
    for i in range(n):
        ja, jb = punto_a_distancia(i, -ventana_m), punto_a_distancia(i, ventana_m)
        if ja == i or jb == i:
            continue
        r1 = rumbo(ruta[ja][0], ruta[ja][1], ruta[i][0], ruta[i][1])
        r2 = rumbo(ruta[i][0], ruta[i][1], ruta[jb][0], ruta[jb][1])
        giro = abs((r2 - r1 + 180) % 360 - 180)
        if angulo_minimo <= giro <= angulo_maximo:
            candidatos.append((dist_acum[i], giro, ruta[i]))

    candidatos.sort(key=lambda c: c[0])
    tramos = []
    for dist, giro, punto in candidatos:
        if tramos and dist - tramos[-1][0] < separacion_minima_m:
            if giro > tramos[-1][1]:
                tramos[-1] = (dist, giro, punto)
        else:
            tramos.append((dist, giro, punto))
    return tramos


def deduplicar_tramos_por_ubicacion(tramos, distancia_min_m=DISTANCIA_MIN_DEDUP_CURVAS_GEO_M):
    """Junta curvas a menos de 'distancia_min_m' en el mundo real (ida y vuelta por la misma
    via, o viajes distintos). Cada tramo es (dist_m, giro, (lat, lon), n_viaje)."""
    unicos = []
    for t in tramos:
        idx = None
        for i, u in enumerate(unicos):
            if distancia_metros(t[2][0], t[2][1], u[2][0], u[2][1]) < distancia_min_m:
                idx = i
                break
        if idx is None:
            unicos.append(t)
        elif t[1] > unicos[idx][1]:
            unicos[idx] = t
    unicos.sort(key=lambda t: (t[3], t[0]))
    return unicos


def detectar_curvas_de_viajes(infos, maximo=MAX_CURVAS_MAPA):
    """Curvas de todos los viajes con ruta real. Devuelve (cantidad_en_bruto, lista_final).
    La lista final no tiene duplicados y, si pasa de 'maximo', conserva solo las de mayor giro
    (siempre en el orden del recorrido)."""
    brutas = []
    for info in infos:
        if not info["ruta_real"]:
            continue
        for dist, giro, punto in detectar_tramos_criticos(info["geometria"]):
            brutas.append((dist, giro, punto, info["indice"]))
    unicas = deduplicar_tramos_por_ubicacion(brutas)
    if maximo and len(unicas) > maximo:
        mejores = sorted(unicas, key=lambda t: -t[1])[:maximo]
        unicas = sorted(mejores, key=lambda t: (t[3], t[0]))
    return len(brutas), unicas




def _archivo_cache_overpass(query):
    try:
        from django.conf import settings
        carpeta = Path(settings.MEDIA_ROOT) / "cache_overpass"
        carpeta.mkdir(parents=True, exist_ok=True)
        return carpeta / f"{hashlib.sha1(query.encode('utf-8')).hexdigest()}.json"
    except Exception:
        return None


def consultar_overpass(query, dias_cache=OVERPASS_DIAS_CACHE):
    """Ejecuta una consulta Overpass. Guarda la respuesta en disco (misma consulta =
    misma respuesta por 'dias_cache' dias) y reintenta con los dos servidores si el
    publico esta saturado (429/502/503/504) o se agota el tiempo. Lanza ErrorOverpass."""
    archivo = _archivo_cache_overpass(query)
    if archivo is not None and archivo.exists():
        if time.time() - archivo.stat().st_mtime < dias_cache * 86400:
            try:
                return json.loads(archivo.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                pass

    headers = {"User-Agent": "ICON-LTDA-reportes/1.0 (uso interno)", "Accept": "application/json"}
    ultimo_error = None
    for ronda in range(2):
        for url in OVERPASS_ENDPOINTS:
            try:
                resp = requests.post(url, data={"data": query}, headers=headers, timeout=80)
                if resp.status_code in (429, 502, 503, 504):
                    raise ErrorOverpass(f"{url} respondió {resp.status_code}")
                resp.raise_for_status()
                data = resp.json()
                remark = str(data.get("remark", "")).lower()
                if "timed out" in remark or "out of memory" in remark:
                    raise ErrorOverpass(f"{url}: {data.get('remark')}")
                if archivo is not None:
                    try:
                        archivo.write_text(json.dumps(data), encoding="utf-8")
                    except OSError:
                        pass
                return data
            except Exception as e:
                ultimo_error = e
                log.warning("Overpass falló en %s: %s", url, e)
        if ronda == 0:
            time.sleep(4)
    raise ErrorOverpass(str(ultimo_error))




def _adelgazar(ruta, paso_m=100):
    """Se queda con un punto cada ~paso_m metros, para no comparar miles de puntos."""
    if not ruta:
        return []
    salida = [ruta[0]]
    acumulado = 0.0
    for a, b in zip(ruta, ruta[1:]):
        acumulado += distancia_metros(a[0], a[1], b[0], b[1])
        if acumulado >= paso_m:
            salida.append(b)
            acumulado = 0.0
    if salida[-1] != ruta[-1]:
        salida.append(ruta[-1])
    return salida


def _tipo_de_punto(tags):
    """Clasifica un elemento de OSM. Cruz Roja se reconoce por el nombre (casi nunca
    esta como amenity=hospital) y las glorietas por junction=roundabout."""
    nombre = _sin_acentos(tags.get("name", ""))
    if "cruz roja" in nombre or "red cross" in nombre:
        return "red_cross"
    if tags.get("junction") == "roundabout":
        return "roundabout"
    return tags.get("amenity") or tags.get("barrier") or tags.get("highway") or "?"


def buscar_puntos_riesgo(coords_ruta, radio_metros=500):
    """Hospitales, bomberos, policía, Cruz Roja, colegios/universidades, estaciones de
    servicio, peajes, radares y glorietas cerca de la ruta. Incluye puntos y edificios
    (muchos hospitales y colegios están mapeados como polígonos, no como puntos). Cada tipo
    tiene su propio radio (RADIO_POR_TIPO); 'radio_metros' aplica a los que no estén ahí."""
    ruta = _adelgazar(coords_ruta)
    if not ruta:
        return []
    lats, lons = [p[0] for p in ruta], [p[1] for p in ruta]
    margen = 0.05  
    bbox = f"{min(lats) - margen},{min(lons) - margen},{max(lats) + margen},{max(lons) + margen}"

    filtros = [
        '["amenity"="hospital"]',
        '["amenity"="school"]',
        '["amenity"="university"]',
        '["amenity"="college"]',
        '["amenity"="fuel"]',
        '["amenity"="fire_station"]',
        '["amenity"="police"]',
        '["barrier"="toll_booth"]',
        '["highway"="speed_camera"]',
        '["junction"="roundabout"]',
        '["name"~"Cruz Roja",i]',
    ]
    partes = "".join(f"nwr{f}({bbox});" for f in filtros)
    query = f"[out:json][timeout:60];({partes});out center tags;"
    data = consultar_overpass(query)

    resultados = []
    for el in data.get("elements", []):
        if el["type"] == "node":
            lat, lon = el["lat"], el["lon"]
        else:
            centro = el.get("center")
            if not centro:
                continue
            lat, lon = centro["lat"], centro["lon"]
        tags = el.get("tags", {})
        tipo = _tipo_de_punto(tags)
        dist_min = min(distancia_metros(lat, lon, plat, plon) for plat, plon in ruta)
        if dist_min <= RADIO_POR_TIPO.get(tipo, radio_metros):
            resultados.append({
                "lat": lat, "lon": lon,
                "nombre": tags.get("name", "(sin nombre)"),
                "tipo": tipo,
                "distancia_a_ruta_m": round(dist_min),
                "telefono": tags.get("phone") or tags.get("contact:phone") or "",
                "direccion_osm": " ".join(
                    x for x in (tags.get("addr:street"), tags.get("addr:housenumber")) if x),
            })
    return resultados




def generar_mapa_html(infos, maniobras, puntos_riesgo, curvas, umbral_km, titulo, avisos=()):
    """Devuelve el HTML completo del mapa, o None si no hay nada que dibujar.

    infos: salida de preparar_viaje().  maniobras: RegistroGPS.  curvas: salida de
    detectar_curvas_de_viajes()[1].  avisos: textos que se muestran en el recuadro del titulo."""
    import folium
    from folium.plugins import MarkerCluster

    todos = [p for i in infos for p in i["geometria"]]
    for m in maniobras:
        c = parsear_coordenadas(m.coordenadas_inicio)
        if c:
            todos.append(c)
    if not todos:
        return None

    
    mapa = folium.Map(
        location=[sum(p[0] for p in todos) / len(todos), sum(p[1] for p in todos) / len(todos)],
        zoom_start=12,
        tiles=None,
    )
    folium.TileLayer(
        tiles="https://server.arcgisonline.com/ArcGIS/rest/services/World_Street_Map/MapServer/tile/{z}/{y}/{x}",
        attr="Esri", name="Mapa", control=False,
    ).add_to(mapa)

    
    for info in infos:
        v, color, n = info["viaje"], info["color"], info["indice"]
        dist_filpac = float(v.distancia_total) if v.distancia_total is not None else None
        etiqueta = (f'<span style="color:{color}; font-size:14px;">&#9632;</span> '
                    f'Viaje {n}: {_hora(v.fecha_hora_inicio)}-{_hora(v.fecha_hora_fin)} · '
                    f'{_num(dist_filpac, 1)} km')
        grupo = folium.FeatureGroup(name=etiqueta, show=True)

        if info["destino"] is None:
            aviso = "<br><i>Sin destino conocido (ultimo movimiento importado): solo se marca la salida.</i>"
        else:
            aviso = ""
            dist_osrm = info["distancia_osrm_km"]
            if info["ruta_real"] and dist_filpac and dist_osrm is not None:
                dif = abs(dist_osrm - dist_filpac) / dist_filpac * 100
                if dif > TOLERANCIA_DISTANCIA_OSRM_PCT:
                    aviso += (f"<br><b style='color:#b00'>Ojo:</b> OSRM da {dist_osrm:.1f} km vs "
                              f"{dist_filpac:.1f} km de FILPAC ({dif:.0f}% de diferencia); "
                              f"el vehiculo pudo tomar otra via.")
            if not info["ruta_real"]:
                aviso += "<br><b style='color:#b00'>Linea recta:</b> OSRM no respondio; se reintenta al recargar."

        popup_html = (
            f"<b>Viaje {n}</b><br>"
            f"{_fecha_hora(v.fecha_hora_inicio)} &rarr; {_hora(v.fecha_hora_fin)}<br>"
            f"Duracion: {html.escape(str(v.duracion_total or '-'))} · FILPAC: {_num(dist_filpac)} km<br>"
            f"Vel. max: {_num(v.velocidad_maxima, 0)} km/h · prom: {_num(v.velocidad_promedio, 0)} km/h<br>"
            f"Desde: {html.escape(v.direccion_origen or '-')}<br>"
            f"Hasta: {html.escape(v.direccion_destino or '-')}{aviso}"
        )

        if len(info["geometria"]) > 1:
            folium.PolyLine(
                info["geometria"], color=color, weight=5, opacity=0.85,
                dash_array=None if info["ruta_real"] else "8 8",
                tooltip=f"Viaje {n} ({_hora(v.fecha_hora_inicio)}-{_hora(v.fecha_hora_fin)})",
                popup=folium.Popup(popup_html, max_width=380),
            ).add_to(grupo)

        folium.CircleMarker(
            info["origen"], radius=6, color="#1a7f1a", fill=True, fill_color="#1a7f1a", fill_opacity=1,
            tooltip=f"Viaje {n} - salida {_hora(v.fecha_hora_inicio)}",
            popup=folium.Popup(popup_html, max_width=380) if info["destino"] is None else None,
        ).add_to(grupo)
        if info["destino"] is not None:
            folium.CircleMarker(
                info["destino"], radius=6, color="#b00020", fill=True, fill_color="#b00020", fill_opacity=1,
                tooltip=f"Viaje {n} - llegada {_hora(v.fecha_hora_fin)}",
            ).add_to(grupo)
        grupo.add_to(mapa)

    
    if maniobras:
        g_man = folium.FeatureGroup(
            name=f"Maniobras menores ({len(maniobras)}, &lt; {umbral_km * 1000:.0f} m)", show=False)
        for m in maniobras:
            c = parsear_coordenadas(m.coordenadas_inicio)
            if not c:
                continue
            metros = float(m.distancia) * 1000 if m.distancia is not None else 0
            folium.CircleMarker(
                c, radius=4, color="#666", fill=True, fill_color="#aaa", fill_opacity=0.9,
                popup=(f"{_fecha_hora(m.inicio)} &rarr; {_hora(m.fin)}<br>{metros:.0f} m<br>"
                       f"{html.escape(m.direccion_inicio or '-')}"),
            ).add_to(g_man)
        g_man.add_to(mapa)

    
    iconos = {
        "hospital": ("red", "plus-sign", "Hospitales"),
        "red_cross": ("lightred", "heart", "Cruz Roja"),
        "school": ("orange", "book", "Colegios y universidades"),
        "university": ("orange", "education", "Colegios y universidades"),
        "college": ("orange", "education", "Colegios y universidades"),
        "fuel": ("blue", "tint", "Estaciones de servicio"),
        "fire_station": ("darkred", "fire", "Bomberos"),
        "police": ("darkblue", "star", "Policia"),
        "toll_booth": ("purple", "usd", "Peajes"),
        "speed_camera": ("black", "camera", "Radares"),
    }
    grupos_riesgo = {}
    for p in puntos_riesgo:
        if p["tipo"] == "roundabout":
            continue  
        color, icono, nombre_capa = iconos.get(p["tipo"], ("gray", "info-sign", "Otros"))
        if nombre_capa not in grupos_riesgo:
            grupos_riesgo[nombre_capa] = folium.FeatureGroup(name=nombre_capa, show=True)
        detalle = f"{html.escape(p['tipo'].upper())}: {html.escape(p['nombre'])} ({p['distancia_a_ruta_m']} m de la ruta)"
        if p.get("telefono"):
            detalle += f"<br>Tel. {html.escape(p['telefono'])}"
        folium.Marker(
            [p["lat"], p["lon"]],
            popup=folium.Popup(detalle, max_width=260),
            icon=folium.Icon(color=color, icon=icono),
        ).add_to(grupos_riesgo[nombre_capa])
    for g in grupos_riesgo.values():
        g.add_to(mapa)

    
    if curvas:
        g_curvas = folium.FeatureGroup(
            name=f'<span style="color:#FFC400">&#9650;</span> Curvas cerradas ({len(curvas)})', show=True)
        grupo_curvas = MarkerCluster(
            options={"maxClusterRadius": 50, "disableClusteringAtZoom": 15, "showCoverageOnHover": False},
        ).add_to(g_curvas)
        for n, (dist, giro, punto, n_viaje) in enumerate(curvas, 1):
            folium.Marker(
                [punto[0], punto[1]],
                tooltip=f"Curva {n} · giro {giro:.0f}°",
                popup=f"Curva {n} (Viaje {n_viaje}, km {dist / 1000:.2f} del viaje, giro de {giro:.0f} grados)",
                icon=folium.DivIcon(
                    html=('<div style="position:relative; width:22px; height:22px;">'
                          '<div style="font-size:22px; line-height:22px; text-align:center; '
                          'color:#FFC400; text-shadow:0 0 2px #000, 0 0 2px #000;">&#9650;</div>'
                          '<div style="position:absolute; left:20px; top:-4px; font:bold 14px system-ui; '
                          'color:#111; text-shadow:-1px -1px 0 #fff, 1px -1px 0 #fff, '
                          f'-1px 1px 0 #fff, 1px 1px 0 #fff;">{n}</div></div>'),
                    icon_size=(22, 22), icon_anchor=(11, 11),
                ),
            ).add_to(grupo_curvas)
        g_curvas.add_to(mapa)

    mapa.fit_bounds([[min(p[0] for p in todos), min(p[1] for p in todos)],
                     [max(p[0] for p in todos), max(p[1] for p in todos)]])

    
    folium.LayerControl(collapsed=True).add_to(mapa)
    mapa.get_root().header.add_child(folium.Element(
        "<style>"
        ".leaflet-control-layers{font:12px system-ui,sans-serif;}"
        ".leaflet-control-layers-expanded{max-height:55vh;overflow-y:auto;padding:4px 8px;}"
        ".leaflet-control-layers label{margin:0;}"
        ".leaflet-control-layers-selector{margin:0 4px 0 0;}"
        "</style>"))

    # Recuadro con el titulo y los avisos (por ejemplo, si Overpass no respondio)
    cuerpo = html.escape(titulo) + "".join(f"<br><span style='color:#b00'>{html.escape(a)}</span>" for a in avisos)
    mapa.get_root().html.add_child(folium.Element(
        '<div style="position:fixed; top:10px; left:60px; z-index:9999; background:white; '
        'padding:6px 10px; border-radius:6px; box-shadow:0 1px 4px rgba(0,0,0,.4); '
        f'font:13px system-ui, sans-serif; max-width:420px;">{cuerpo}</div>'))

    return mapa.get_root().render()