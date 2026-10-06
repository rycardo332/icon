import logging
import os
from concurrent.futures import ThreadPoolExecutor
import unicodedata
from datetime import date, datetime, timedelta
from pathlib import Path

import openpyxl
from django.conf import settings
from openpyxl.comments import Comment
from openpyxl.drawing.image import Image
from openpyxl.drawing.spreadsheet_drawing import AnchorMarker, TwoCellAnchor
from openpyxl.styles import Alignment
from .instrucciones_ruta import lineas_de_ruta
from .entorno_ruta import ERRORES_ENTORNO, buscar_entorno_ruta, buscar_superficie_via
from .geocodificacion import direccion_de_coordenada, texto_ubicacion
from .mapa_png import datos_mapa_de_viajes, generar_mapa_png_viajes
from .mapa_viajes import ErrorOverpass, distancia_metros

log = logging.getLogger(__name__)


MAPA_COL_INI, MAPA_COL_FIN = 1, 11
MAPA_FILA_INI, MAPA_FILA_FIN = 20, 26


VISIBILIDAD_CELDAS = {
    "buena": "A14",
    "niebla": "C14",
    "curvas": "E14",
    "material_particulado": "G14",
    "iluminacion": "J14",
}
TRAFICO_PREFIJOS = {
    "camiones": "MAYORÍA CAMIONES",
    "automoviles": "MAYORÍA AUTOMÓVILES",
    "mixto": "MIXTO",
}
SUPERFICIE_PREFIJOS = {
    "pavimentada": "PAVIMENTADA",
    "trocha": "TROCHA",
    "con_huecos": "CON HUECOS",
    "senalizada": "SEÑALIZADA",
    "demarcada": "DEMARCADA",
    "amplia": "AMPLIA",
    "angosta": "ANGOSTA",
}
CONDICION_PREFIJOS = {
    "inestabilidad": "INESTAB. GEOLÓGICA",
    "caida_bancada": "CAÍDA DE BANCADA",
    "hundimientos": "HUNDIMIENTOS",
    "zona_inundable": "ZONA INUNDABLE",
}

MESES_ES = {1: "ENERO", 2: "FEBRERO", 3: "MARZO", 4: "ABRIL", 5: "MAYO", 6: "JUNIO",
            7: "JULIO", 8: "AGOSTO", 9: "SEPTIEMBRE", 10: "OCTUBRE", 11: "NOVIEMBRE",
            12: "DICIEMBRE"}

FILA_MES = {nombre: 11 + num for num, nombre in MESES_ES.items()}


FILAS_TRAMOS = 7
FILAS_ZONAS = 8
FILAS_AGUA = 2
FILAS_APOYO = 8

SIN_NOMBRE = "(sin nombre)"
ESCOLARES = ("school", "college", "university")


DIST_ESCOLAR_CRITICO_M = 150
DIST_GLORIETA_CRITICA_M = 60
MAX_ESCOLARES_CRITICOS = 2
MAX_GLORIETAS_CRITICAS = 2


RADIO_MUNICIPIO_M = 6000
ETIQUETAS_APOYO = {
    "fire_station": "BOMBEROS",
    "police": "POLICÍA NACIONAL",
    "hospital": "HOSPITAL",
    "red_cross": "CRUZ ROJA",
}

APOYO_DEFECTO = {
    "fire_station": {"telefono": "119", "equipos": "Equipo de bomberos", "responsable": "Personal de turno"},
    "police": {"telefono": "123", "equipos": "Patrulla", "responsable": "Personal de turno"},
    "hospital": {"telefono": "", "equipos": "Equipo médico", "responsable": "Personal de turno"},
    "red_cross": {"telefono": "132", "equipos": "Paramédico - Ambulancia", "responsable": "Personal de turno"},
}
ORDEN_ENTIDAD = ("bombero", "polic", "cruz roja", "hospital")  # orden de las filas, como en el original


def formatear_duracion(valor):
    """Convierte un timedelta en '1h 30m'. Si ya es texto, lo deja igual."""
    if hasattr(valor, "total_seconds"):
        total_min = int(valor.total_seconds() // 60)
        horas, minutos = divmod(total_min, 60)
        return f"{horas}h {minutos:02d}m"
    return str(valor) if valor else ""


def _norm(texto):
    """Minúsculas y sin tildes, para comparar nombres."""
    t = unicodedata.normalize("NFD", str(texto or ""))
    return "".join(c for c in t if unicodedata.category(c) != "Mn").lower().strip()


def _celdas_con_formula(ws):
    """Coordenadas de las celdas que ya traen una fórmula en la plantilla (las legítimas),
    más la columna P del resumen mensual, donde el código escribe fórmulas a propósito."""
    legitimas = {c.coordinate for fila in ws.iter_rows() for c in fila if c.data_type == "f"}
    legitimas.update(f"P{fila}" for fila in FILA_MES.values())
    return legitimas


def _neutralizar_formulas(ws, legitimas):
    """Un nombre o dirección que empiece por '=' (viene de un Excel subido o de OpenStreetMap,
    que edita cualquiera) lo guardaría openpyxl como fórmula, y se ejecutaría al abrir el
    reporte. Aquí toda celda con fórmula que no sea de la plantilla se deja como texto."""
    for fila in ws.iter_rows():
        for c in fila:
            if c.data_type == "f" and c.coordinate not in legitimas:
                c.data_type = "s"


def _tam_area_px(ws):
    """Tamaño aproximado en píxeles de A20:K26, para que el mapa no salga deformado."""
    ancho_def = ws.sheet_format.defaultColWidth or 8.43
    anchos = {}
    for letra, dim in ws.column_dimensions.items():
        ini = dim.min or openpyxl.utils.column_index_from_string(letra)
        fin = dim.max or ini
        for i in range(ini, fin + 1):
            anchos[i] = dim.width or ancho_def
    ancho = sum(int(anchos.get(c, ancho_def) * 7 + 5)
                for c in range(MAPA_COL_INI, MAPA_COL_FIN + 1))
    alto_pt = sum(ws.row_dimensions[r].height or 15
                  for r in range(MAPA_FILA_INI, MAPA_FILA_FIN + 1))
    return ancho, int(alto_pt * 96 / 72)


def _igualar_filas_mapa(ws):
    """Reparte en partes iguales la altura de las filas del mapa (20-26), para que los
    meses de la tabla M-P que caen ahí no queden en filas desparejas. El total no
    cambia, así que el mapa conserva su tamaño."""
    filas = range(MAPA_FILA_INI, MAPA_FILA_FIN + 1)
    total = sum(ws.row_dimensions[r].height or 15 for r in filas)
    for r in filas:
        ws.row_dimensions[r].height = total / len(filas)


def _escribir(ws, celda, valor, envolver=False):
    """Escribe en la celda ancla (esquina superior izquierda) sin tocar las combinadas."""
    ws[celda].value = valor
    if envolver:
        al = ws[celda].alignment
        ws[celda].alignment = Alignment(
            horizontal=al.horizontal or "center", vertical="center", wrap_text=True
        )


def _marcar_celda_unica(ws, celda):
    """Cambia el guion final de una celda de una sola opción ('ETIQUETA   _____')
    por '__X__', sin tocar el resto del texto."""
    texto = ws[celda].value or ""
    if texto.rstrip().endswith("_____"):
        ws[celda].value = texto.rstrip()[:-5] + "__X__"


def _marcar_multilinea(ws, celda, prefijos_seleccionados):
    """Para una celda con varias opciones (una por línea, 'ETIQUETA   _____'),
    marca con X solo las líneas cuyo inicio coincide con alguno de los prefijos."""
    texto = ws[celda].value or ""
    lineas = texto.split("\n")
    prefijos = [p.upper() for p in prefijos_seleccionados]
    for i, linea in enumerate(lineas):
        mayus = linea.upper()
        if any(mayus.startswith(p) for p in prefijos) and linea.rstrip().endswith("_____"):
            lineas[i] = linea.rstrip()[:-5] + "__X__"
    ws[celda].value = "\n".join(lineas)


def _llenar_condiciones_via(ws, ruta):
    """Marca con X las casillas de las secciones 2 y 3, según lo guardado en la
    Ruta desde la pantalla 'Condiciones de la Vía'."""
    for valor in (ruta.visibilidad or []):
        celda = VISIBILIDAD_CELDAS.get(valor)
        if celda:
            _marcar_celda_unica(ws, celda)

    if ruta.trafico:
        prefijo = TRAFICO_PREFIJOS.get(ruta.trafico)
        if prefijo:
            _marcar_multilinea(ws, "A18", [prefijo])

    prefijos_superficie = [
        SUPERFICIE_PREFIJOS[v] for v in (ruta.tipo_superficie or []) if v in SUPERFICIE_PREFIJOS
    ]
    if prefijos_superficie:
        _marcar_multilinea(ws, "C18", prefijos_superficie)

    prefijos_condicion = [
        CONDICION_PREFIJOS[v] for v in (ruta.condiciones_generales or []) if v in CONDICION_PREFIJOS
    ]
    if prefijos_condicion:
        _marcar_multilinea(ws, "E18", prefijos_condicion)

    _marcar_multilinea(ws, "G18", ["SI"] if ruta.tiene_restriccion_horario else ["NO"])
    if ruta.tiene_restriccion_horario:
        if ruta.restriccion_desde:
            ws["H18"].value = ruta.restriccion_desde.strftime("%H:%M")
        if ruta.restriccion_hasta:
            ws["I18"].value = ruta.restriccion_hasta.strftime("%H:%M")
        if ruta.restriccion_dias:
            ws["J18"].value = ruta.restriccion_dias


def _perfiles_de_ruta(infos):
    """{n_viaje: (geometria, distancias_acumuladas_m)} de cada viaje con ruta real."""
    perfiles = {}
    for i in infos:
        g = i["geometria"]
        if not i["ruta_real"] or len(g) < 2:
            continue
        acum = [0.0]
        for a, b in zip(g, g[1:]):
            acum.append(acum[-1] + distancia_metros(a[0], a[1], b[0], b[1]))
        perfiles[i["indice"]] = (g, acum)
    return perfiles


def _ubicar(perfiles, lat, lon):
    """(n_viaje, metros_desde_el_inicio, distancia_a_la_ruta_m) en el viaje cuya ruta pasa
    mas cerca del punto. None si no hay ninguna ruta."""
    mejor = None
    for n, (g, acum) in perfiles.items():
        k, d = min(((k, distancia_metros(lat, lon, p[0], p[1])) for k, p in enumerate(g)),
                   key=lambda t: t[1])
        if mejor is None or d < mejor[2]:
            mejor = (n, acum[k], d)
    return mejor


def _orden_en_ruta(perfiles, p):
    u = _ubicar(perfiles, p["lat"], p["lon"])
    return (u[0], u[1]) if u else (99, 0.0)


def _tiene_nombre(p):
    return bool(p.get("nombre")) and p["nombre"] != SIN_NOMBRE


def _sin_repetidos_por_nombre(puntos):
    """Deja el primero de cada nombre (OSM suele traer varios edificios del mismo colegio)."""
    vistos, salida = set(), []
    for p in puntos:
        clave = _norm(p["nombre"])
        if clave not in vistos:
            vistos.add(clave)
            salida.append(p)
    return salida


# ----------------------------------------------------------------------
# Tabla 5: puntos criticos
# ----------------------------------------------------------------------

# Texto de la columna OBSERVACIONES de la tabla 5, según el tráfico marcado en la Ruta
TRAFICO_OBSERVACION = {
    "camiones": "Transporte de carga",
    "automoviles": "Tránsito de vehículos livianos",
    "mixto": "Transporte intermunicipal y de carga",
}


def _sentido_de_viaje(info):
    """'Duitama hacia Sogamoso', con el municipio de salida y de llegada del viaje."""
    v = info["viaje"]

    def lugar(punto, respaldo):
        if punto:
            geo = direccion_de_coordenada(punto[0], punto[1]) or {}
            if geo.get("municipio"):
                return geo["municipio"]
        return respaldo or ""

    o = lugar(info["origen"], v.direccion_origen)
    d = lugar(info["destino"], v.direccion_destino) if info["destino"] else ""
    if o and d and o == d:
        return f"Recorrido dentro de {o}"
    if o and d:
        return f"{o} hacia {d}"
    return o or d or "-"


def _armar_criticos(curvas, riesgos, perfiles):
    """Elige y numera los puntos de la tabla 5 (maximo FILAS_TRAMOS). Mezcla:
      - hasta 2 zonas escolares (colegio a <= 150 m de la via, con nombre),
      - hasta 2 glorietas por las que pasa la ruta,
      - el resto, las curvas mas cerradas (si faltan curvas, mas colegios/glorietas).
    Se ordenan en el sentido del recorrido y se numeran 1..n; ese numero es el que lleva el
    triangulo en el mapa. Devuelve lista de dicts: n, tipo, lat, lon, n_viaje, nombre, dist_m,
    giro, km."""
    escolares = _sin_repetidos_por_nombre(sorted(
        (p for p in riesgos if p["tipo"] in ESCOLARES and _tiene_nombre(p)
         and p["distancia_a_ruta_m"] <= DIST_ESCOLAR_CRITICO_M),
        key=lambda p: p["distancia_a_ruta_m"]))

    glorietas = []
    for p in sorted((p for p in riesgos if p["tipo"] == "roundabout"
                     and p["distancia_a_ruta_m"] <= DIST_GLORIETA_CRITICA_M),
                    key=lambda p: p["distancia_a_ruta_m"]):
        # una glorieta a veces viene partida en varios tramos: se junta lo que esta a < 60 m
        if all(distancia_metros(p["lat"], p["lon"], g["lat"], g["lon"]) >= 60 for g in glorietas):
            glorietas.append(p)

    esc_sel = escolares[:MAX_ESCOLARES_CRITICOS]
    glo_sel = glorietas[:MAX_GLORIETAS_CRITICAS]
    n_curvas = max(0, FILAS_TRAMOS - len(esc_sel) - len(glo_sel))
    curvas_sel = sorted(curvas, key=lambda c: -c[1])[:n_curvas]

    sobran = FILAS_TRAMOS - len(esc_sel) - len(glo_sel) - len(curvas_sel)
    if sobran > 0:
        extras = ([("escolar", p) for p in escolares[len(esc_sel):]] +
                  [("glorieta", p) for p in glorietas[len(glo_sel):]])[:sobran]
    else:
        extras = []

    items = []
    for dist, giro, punto, n_viaje in curvas_sel:
        items.append({"tipo": "curva", "lat": punto[0], "lon": punto[1], "n_viaje": n_viaje,
                      "orden": dist, "km": dist / 1000, "giro": giro, "nombre": "", "dist_m": 0})
    for tipo, p in ([("escolar", p) for p in esc_sel] + [("glorieta", p) for p in glo_sel] + extras):
        u = _ubicar(perfiles, p["lat"], p["lon"])
        if u is None:
            continue
        items.append({"tipo": tipo, "lat": p["lat"], "lon": p["lon"], "n_viaje": u[0],
                      "orden": u[1], "km": u[1] / 1000, "giro": 0,
                      "nombre": p["nombre"] if _tiene_nombre(p) else "", "dist_m": p["distancia_a_ruta_m"]})

    items.sort(key=lambda it: (it["n_viaje"], it["orden"]))
    for n, it in enumerate(items, 1):
        it["n"] = n
    return items


def _llenar_tramos_criticos(ws, criticos, infos, ruta_obj):
    """'5. TRAMOS E INTERSECCIONES CRÍTICAS' (filas 42-48, 7 filas). 'criticos' es la salida
    de _armar_criticos: curvas cerradas, zonas escolares y glorietas.

    El número de cada fila es el mismo que lleva el triángulo en el mapa, para que quien
    revise pueda ubicarlo en el plano. Los datos técnicos (giro, km, coordenada) quedan
    como comentario de la celda A, no en la tabla."""
    por_indice = {i["indice"]: i for i in infos}
    sentidos = {}
    observacion = TRAFICO_OBSERVACION.get(ruta_obj.trafico, "Tránsito vehicular")

    for it in criticos[:FILAS_TRAMOS]:
        fila = 42 + it["n"] - 1
        n_viaje = it["n_viaje"]
        if n_viaje not in sentidos:
            info = por_indice.get(n_viaje)
            sentidos[n_viaje] = _sentido_de_viaje(info) if info else "-"

        calle = texto_ubicacion(it["lat"], it["lon"])
        coord = f"Coordenada: {it['lat']:.5f}, {it['lon']:.5f}"
        if it["tipo"] == "curva":
            titulo, ubicacion = f"CURVA {it['n']} - PRECAUCIÓN CURVA CERRADA", calle
            detalle = f"Giro de {it['giro']:.0f}°, km {it['km']:.2f} del viaje {n_viaje}\n{coord}"
        elif it["tipo"] == "glorieta":
            titulo = f"GLORIETA {it['n']} - PRECAUCIÓN GLORIETA"
            ubicacion = f"{it['nombre']}, {calle}" if it["nombre"] else calle
            detalle = f"Km {it['km']:.2f} del viaje {n_viaje}\n{coord}"
        else:
            titulo = f"ZONA ESCOLAR {it['n']} - PRECAUCIÓN ZONA ESCOLAR"
            ubicacion = f"{it['nombre']}, {calle}" if it["nombre"] else calle
            detalle = f"A {it['dist_m']} m de la vía, km {it['km']:.2f} del viaje {n_viaje}\n{coord}"

        ws[f"A{fila}"].value = titulo
        ws[f"D{fila}"].value = ubicacion
        ws[f"G{fila}"].value = sentidos[n_viaje]
        ws[f"I{fila}"].value = observacion
        ws[f"A{fila}"].comment = Comment(detalle, "Sistema")


# ----------------------------------------------------------------------
# Tabla 6: zonas urbanas y/o escolares
# ----------------------------------------------------------------------

def _direccion_de_punto(p):
    """Dirección de un punto de Overpass: la que trae OSM (addr:street) o, si no,
    la de la geocodificación inversa."""
    return p.get("direccion_osm") or texto_ubicacion(p["lat"], p["lon"])


def _elegir_zonas(riesgos, lugares, perfiles):
    """Filas de la tabla 6 (maximo FILAS_ZONAS), ordenadas en el sentido del recorrido:
    hasta 2 ciudades/municipios y 2 veredas/caserios, y el resto colegios y universidades
    con nombre (sin repetir). Devuelve lista de ('lugar' | 'colegio', punto)."""
    urbanos = [l for l in lugares if l["tipo"] in ("city", "town")]
    poblados = [l for l in lugares if l["tipo"] in ("village", "hamlet")]
    base = urbanos[:2] + poblados[:2]
    resto_lugares = urbanos[2:] + poblados[2:]

    colegios = _sin_repetidos_por_nombre(sorted(
        (p for p in riesgos if p["tipo"] in ESCOLARES and _tiene_nombre(p)),
        key=lambda p: p["distancia_a_ruta_m"]))

    filas = [("lugar", p) for p in base]
    filas += [("colegio", p) for p in colegios[:FILAS_ZONAS - len(filas)]]
    if len(filas) < FILAS_ZONAS:
        filas += [("lugar", p) for p in resto_lugares[:FILAS_ZONAS - len(filas)]]
    filas.sort(key=lambda f: _orden_en_ruta(perfiles, f[1]))
    return filas


def _llenar_zonas_urbanas(ws, zonas):
    """'6. PASO POR ZONAS URBANAS Y/O ESCOLARES' (filas 51-58, 8 filas)."""
    fila_ini = 51
    for i, (clase, p) in enumerate(zonas[:FILAS_ZONAS]):
        fila = fila_ini + i
        ws[f"A{fila}"].value = p["nombre"]
        if clase == "colegio":
            ws[f"E{fila}"].value = _direccion_de_punto(p)
            ws[f"H{fila}"].value = "Zona escolar"
        else:
            ws[f"E{fila}"].value = texto_ubicacion(p["lat"], p["lon"])
            ws[f"H{fila}"].value = p["etiqueta"]


# ----------------------------------------------------------------------
# Tabla 7: cuerpos de agua
# ----------------------------------------------------------------------

def _dividir_lineas(texto, n_campos):
    """Cada línea del texto: 'campo1 | campo2 | campo3...' -> lista de listas de
    tamaño n_campos (rellena con '' lo que falte, corta lo que sobre).
    Si la línea no trae '|', también acepta 'campo1 - campo2 - campo3' como
    respaldo, para el texto escrito a mano en el formulario."""
    filas = []
    for linea in (texto or "").splitlines():
        linea = linea.strip()
        if not linea:
            continue
        if "|" in linea:
            partes = [p.strip() for p in linea.split("|")]
        else:
            partes = [p.strip() for p in linea.split(" - ")]
        partes = (partes + [""] * n_campos)[:n_campos]
        filas.append(partes)
    return filas


def _llenar_cuerpos_de_agua(ws, texto, aguas):
    """'7. PROXIMIDAD A CUERPOS DE AGUA' (filas 61-62: la plantilla solo trae 2).
    Primero van las líneas escritas a mano en la Ruta ('FUENTE | NOMBRE | UBICACIÓN');
    las filas que sobren se llenan con los ríos/quebradas que detecta Overpass
    (primero ríos, luego quebradas, los más cercanos a la ruta primero).
    La ubicación es el puente o la vía del cruce que calcula entorno_ruta."""
    filas = _dividir_lineas(texto, 3)[:FILAS_AGUA]

    manual_minus = (texto or "").lower()
    candidatas = sorted(
        (a for a in aguas if a["nombre"].lower() not in manual_minus),
        key=lambda a: (a["tipo"] != "river", a["distancia_a_ruta_m"]),
    )
    for a in candidatas[:FILAS_AGUA - len(filas)]:
        filas.append([a["etiqueta"], a["nombre"],
                      a.get("ubicacion") or texto_ubicacion(a["lat"], a["lon"])])

    fila_ini = 61
    for i, (fuente, nombre, ubicacion) in enumerate(filas):
        fila = fila_ini + i
        ws[f"A{fila}"].value = fuente
        ws[f"E{fila}"].value = nombre
        ws[f"H{fila}"].value = ubicacion


# ----------------------------------------------------------------------
# Tabla 8: puntos de apoyo para atencion de emergencias
# ----------------------------------------------------------------------

def _telefono_apoyo(tipo, telefono_osm):
    base = APOYO_DEFECTO.get(tipo, {}).get("telefono", "")
    if base and telefono_osm:
        return f"{base} - {telefono_osm}"
    return telefono_osm or base


def _municipios_de_apoyo(lugares, perfiles, n_max):
    """Ciudades por las que pasa la ruta (si no hay, municipios), en el orden del recorrido.
    Si hay mas de las que caben, se quedan las mas cercanas a la ruta."""
    ciudades = [l for l in lugares if l["tipo"] == "city"] or [l for l in lugares if l["tipo"] == "town"]
    elegidas = sorted(ciudades, key=lambda l: l["distancia_a_ruta_m"])[:n_max]
    return sorted(elegidas, key=lambda l: _orden_en_ruta(perfiles, l))


def _mejor_de_tipo(candidatos, centro, usados):
    """El punto del tipo dado mas cercano al centro del municipio (dentro de RADIO_MUNICIPIO_M).
    Prefiere los que tienen nombre; en hospitales, los que dicen 'hospital'."""
    cerca = []
    for p in candidatos:
        if id(p) in usados:
            continue
        d = distancia_metros(centro["lat"], centro["lon"], p["lat"], p["lon"])
        if d <= RADIO_MUNICIPIO_M:
            es_hospital = p["tipo"] == "hospital" and "hospital" in _norm(p.get("nombre"))
            cerca.append(((0 if es_hospital else 1), (0 if _tiene_nombre(p) else 1), d, p))
    if not cerca:
        return None
    return min(cerca, key=lambda t: t[:3])[3]


def _fila_de_apoyo(p, ubicacion=None):
    """[entidad, ubicacion, telefono, equipos, direccion, responsable] de un punto de OSM."""
    defecto = APOYO_DEFECTO.get(p["tipo"], {})
    geo = None
    if not ubicacion or not p.get("direccion_osm"):
        geo = direccion_de_coordenada(p["lat"], p["lon"]) or {}
    ubicacion = ubicacion or (geo or {}).get("municipio") or f"A {p['distancia_a_ruta_m']} m de la ruta"
    direccion = p.get("direccion_osm") or (geo or {}).get("texto") or f"{p['lat']:.5f}, {p['lon']:.5f}"
    # Hospitales: el nombre real ('Hospital Regional de Duitama'); los demás llevan el nombre
    # genérico ('BOMBEROS', 'POLICÍA NACIONAL') y el municipio va en la columna UBICACIÓN.
    if p["tipo"] == "hospital" and _tiene_nombre(p):
        entidad = p["nombre"]
    else:
        entidad = ETIQUETAS_APOYO.get(p["tipo"], "APOYO")
    return [entidad, ubicacion, _telefono_apoyo(p["tipo"], p.get("telefono", "")),
            defecto.get("equipos", ""), direccion, defecto.get("responsable", "")]


def _apoyo_automatico(puntos, cupo, usados):
    """Completa el cupo turnandose entre los tipos y tomando primero los mas cercanos a la ruta,
    con los puntos que no entraron por municipio."""
    if cupo <= 0:
        return []
    por_tipo = {t: sorted((p for p in puntos if p["tipo"] == t and id(p) not in usados),
                          key=lambda p: p["distancia_a_ruta_m"])
                for t in ETIQUETAS_APOYO}
    elegidos = []
    while len(elegidos) < cupo and any(por_tipo.values()):
        for t in ETIQUETAS_APOYO:
            if por_tipo[t] and len(elegidos) < cupo:
                elegidos.append(por_tipo[t].pop(0))
    return elegidos


def _rank_entidad(entidad):
    e = _norm(entidad)
    for i, clave in enumerate(ORDEN_ENTIDAD):
        if clave in e:
            return i
    return len(ORDEN_ENTIDAD)


def _armar_filas_apoyo(texto, riesgos, lugares, perfiles):
    """Filas de la tabla de apoyo. Las líneas escritas a mano en la Ruta van primero (así
    Cruz Roja nunca se pierde), con el formato 'ENTIDAD | UBICACIÓN | TELÉFONO | EQUIPOS |
    DIRECCIÓN | RESPONSABLE'. Las que sobren se llenan, por municipio del recorrido, con
    bomberos, policía, hospital y Cruz Roja de OpenStreetMap; al final todo se agrupa por
    municipio, en el orden Bomberos, Policía, Cruz Roja, Hospital (como el formato original)."""
    filas = _dividir_lineas(texto, 6)[:FILAS_APOYO]
    cupo = FILAS_APOYO - len(filas)
    puntos = [p for p in riesgos if p["tipo"] in ETIQUETAS_APOYO]

    tipos = ["fire_station", "police", "red_cross", "hospital"]
    if "cruz roja" in _norm(texto):
        tipos.remove("red_cross")  # ya la escribieron a mano
    usados = set()

    municipios = _municipios_de_apoyo(lugares, perfiles, max(1, cupo // len(tipos))) if cupo > 0 else []
    for m in municipios:
        for tipo in tipos:
            if len(filas) >= FILAS_APOYO:
                break
            p = _mejor_de_tipo([q for q in puntos if q["tipo"] == tipo], m, usados)
            if p is not None:
                usados.add(id(p))
                filas.append(_fila_de_apoyo(p, ubicacion=m["nombre"]))

    for p in _apoyo_automatico(puntos, FILAS_APOYO - len(filas), usados):
        filas.append(_fila_de_apoyo(p))

    orden_muni = {_norm(m["nombre"]): i for i, m in enumerate(municipios)}
    filas.sort(key=lambda f: (orden_muni.get(_norm(f[1]), 50), _rank_entidad(f[0])))
    return filas[:FILAS_APOYO]


def _llenar_puntos_apoyo(ws, filas):
    """'PUNTOS DE APOYO PARA ATENCIÓN DE EMERGENCIAS' (filas 65-72, 8 filas)."""
    fila_ini = 65
    for i, (entidad, ubicacion, telefono, equipos, direccion, responsable) in enumerate(filas):
        fila = fila_ini + i
        ws[f"A{fila}"].value = entidad
        ws[f"C{fila}"].value = ubicacion
        ws[f"E{fila}"].value = telefono
        ws[f"G{fila}"].value = equipos
        ws[f"H{fila}"].value = direccion
        ws[f"J{fila}"].value = responsable


def _llenar_resumen_mensual(ws, ruta_obj, distancia_km=None, anio=None):
    """Tabla 'NÚMERO DE VIAJES / KM IDA Y VUELTA / TOTAL KM' (columnas M-P).
    Cada Desplazamiento de la ruta cuenta como un viaje del mes. 'KM IDA Y VUELTA' es el
    doble de la distancia de la ruta; el TOTAL KM lo calcula la fórmula (=O*N) de la plantilla.
    Los meses después de julio no vienen en la plantilla: se crean copiando el estilo de JUNIO."""
    from copy import copy

    from .models import Desplazamiento  # import local para evitar dependencia circular

    km_ida_vuelta = round(float(distancia_km or ruta_obj.distancia_km or 0) * 2, 1)

    consulta = Desplazamiento.objects.filter(ruta=ruta_obj)
    if anio:
        consulta = consulta.filter(fecha_desplazamiento__year=anio)
    por_mes = {}
    for d in consulta:
        mes = d.fecha_desplazamiento.month
        por_mes[mes] = por_mes.get(mes, 0) + 1

    fila_modelo = FILA_MES["JUNIO"]
    for mes_num in range(8, max(por_mes, default=0) + 1):
        fila = FILA_MES[MESES_ES[mes_num]]
        for col in "MNOP":
            modelo, destino = ws[f"{col}{fila_modelo}"], ws[f"{col}{fila}"]
            destino.font = copy(modelo.font)
            destino.border = copy(modelo.border)
            destino.fill = copy(modelo.fill)
            destino.alignment = copy(modelo.alignment)
            destino.number_format = modelo.number_format
        ws[f"M{fila}"].value = MESES_ES[mes_num]
        ws[f"P{fila}"].value = f"=N{fila}*O{fila}"

    for mes_num, nombre in MESES_ES.items():
        n = por_mes.get(mes_num, 0)
        if n:
            fila = FILA_MES[nombre]
            ws[f"N{fila}"].value = n
            ws[f"O{fila}"].value = km_ida_vuelta


def generar_excel_tarjeta_ruta(ruta_obj, viajes=None, mapa_png_path=None, con_riesgos=True,
                               desplazamiento=None):
    """
    Llena la plantilla con los datos de la ruta, las condiciones de la vía, las
    tablas de tramos críticos/zonas/cuerpos de agua/puntos de apoyo, el resumen
    mensual, y pega el mapa en "4. PLANO DE LA RUTA".

    - viajes: los Viaje (FILPAC) que se dibujan en el mapa, en orden (lista o queryset).
    - mapa_png_path: si ya tienes un PNG del mapa, se usa ese en vez de generarlo.
    - con_riesgos: incluir hospitales, bomberos, policía, colegios, peajes, radares,
      estaciones, glorietas, y también centros poblados y ríos/quebradas (usa Overpass;
      más lento).
    - desplazamiento: si la Ruta no tiene tiempo/distancia/origen/destino, se toman de
      aqui y del viaje GPS.

    Si Overpass no responde, el Excel se genera igual y el motivo queda como comentario en
    la celda A5 (el triangulito rojo del título) y en el log.
    """
    viajes = list(viajes or [])
    viaje = viajes[0] if viajes else None

    # 1. RUTAS DE ARCHIVOS
    base_dir = Path(settings.BASE_DIR)
    media_root = Path(settings.MEDIA_ROOT)

    plantilla_path = base_dir / "plantillas" / "plantilla_tarjeta_ruta_limpia.xlsx"
    if not os.path.exists(plantilla_path):
        raise FileNotFoundError(f"No se encontró la plantilla en: {plantilla_path}")

    output_dir = media_root / "reportes"
    os.makedirs(output_dir, exist_ok=True)
    sufijo_disp = f"_d{desplazamiento.pk}" if desplazamiento is not None else ""
    salida_path = output_dir / f"Tarjeta_Ruta_{ruta_obj.id}{sufijo_disp}.xlsx"

    # 2. ABRIR PLANTILLA
    wb = openpyxl.load_workbook(plantilla_path)
    ws = wb.active
    formulas_legitimas = _celdas_con_formula(ws)  # para neutralizar las que vengan de texto externo

    ws._charts = []
    _igualar_filas_mapa(ws)
    # 3. DATOS GENERALES DE LA RUTA
    nombre_ruta = ruta_obj.nombre or "TRAMO URBANO / REGIONAL"

    tiempo = ruta_obj.tiempo_estimado
    if not tiempo and viaje is not None and viaje.duracion_total:
        tiempo = viaje.duracion_total
    if not tiempo and desplazamiento is not None:
        try:
            hoy = date.today()
            dif = (datetime.combine(hoy, desplazamiento.hora_llegada_estimada)
                   - datetime.combine(hoy, desplazamiento.hora_salida))
            tiempo = dif if dif > timedelta(0) else None
        except (TypeError, AttributeError):
            tiempo = None
    tiempo_estimado = formatear_duracion(tiempo)

    origen = ruta_obj.origen or (viaje.direccion_origen if viaje is not None else "")
    destino = ruta_obj.destino or (viaje.direccion_destino if viaje is not None else "")

    distancia_km = ruta_obj.distancia_km
    if not distancia_km and viaje is not None:
        distancia_km = viaje.distancia_osrm_km or viaje.distancia_total
    distancia_txt = f"{distancia_km} KM" if distancia_km else ""

    # 4. ENCABEZADO
    _escribir(ws, "A5", f"TARJETA DE RUTA TRAMO ICON LTDA - {nombre_ruta.upper()}")
    _escribir(ws, "A9", nombre_ruta)
    _escribir(ws, "D9", tiempo_estimado)      # D9:E9   TIEMPO ESTIMADO
    _escribir(ws, "F9", origen)               # F9:G9   DESDE
    _escribir(ws, "H9", destino)              # H9:I9   HASTA
    _escribir(ws, "J9", distancia_txt)        # J9:K9   DISTANCIA EN Km.

    resumen = getattr(ruta_obj, "descripcion_ruta", "") or ""
    if not resumen:
        lineas = lineas_de_ruta(viajes) if viajes else []
        if lineas:
            resumen = "\n\n".join(lineas)
        else:
            resumen = f"{origen} → {destino}" if origen and destino else nombre_ruta
            if distancia_txt:
                resumen += f"  ·  {distancia_txt}"
            if tiempo_estimado:
                resumen += f"  ·  {tiempo_estimado} aprox."
    _escribir(ws, "A12", resumen, envolver=True)  # A12:K12  1. RUTA PRINCIPAL

    # 5. DATOS DERIVADOS DEL MAPA (curvas, puntos de riesgo/apoyo, entorno). Se calculan una
    # sola vez y se usan tanto para el PNG como para las tablas 5 a 8.
    avisos = []
    infos, curvas, riesgos = [], [], []
    cache_usado = bool(
        desplazamiento is not None
        and desplazamiento.cache_entorno
        and desplazamiento.cache_entorno.get("completo")
    )

    if viajes:
        infos, curvas, riesgos = datos_mapa_de_viajes(
            viajes, con_riesgos=(con_riesgos and not cache_usado), avisos=avisos
        )

    lugares, aguas = [], []
    if cache_usado:
        cache = desplazamiento.cache_entorno
        lugares = cache.get("lugares", [])
        aguas = cache.get("aguas", [])
        riesgos = cache.get("riesgos", riesgos) or riesgos
        log.info("[EXCEL] usando cache_entorno del desplazamiento %s", desplazamiento.pk)

    if con_riesgos and not cache_usado:
        coords = [p for i in infos if i["ruta_real"] for p in i["geometria"]]
        if coords:
            necesita_superficie = not ruta_obj.tipo_superficie
            with ThreadPoolExecutor(max_workers=2) as pool:
                f_entorno = pool.submit(buscar_entorno_ruta, coords, radio_agua_m=400)
                f_superficie = pool.submit(buscar_superficie_via, coords) if necesita_superficie else None

                try:
                    lugares, aguas = f_entorno.result()
                except ErrorOverpass as e:
                    log.warning("Sin centros poblados ni cuerpos de agua: Overpass no respondió (%s)", e)
                    avisos.append("No se pudieron consultar centros poblados ni ríos/quebradas "
                                  f"(Overpass no respondió: {e}). Vuelve a generar el Excel en unos minutos.")
                else:
                    for err in ERRORES_ENTORNO:
                        avisos.append(f"Consulta incompleta a Overpass: {err[:200]}")

                    cache_previo = (desplazamiento.cache_entorno or {}) if desplazamiento is not None else {}
                    error_lugares = any(e.startswith("centros poblados") for e in ERRORES_ENTORNO)
                    error_aguas = any(
                        e.startswith("cuerpos de agua") or e.startswith("ubicación de cuerpos de agua")
                        for e in ERRORES_ENTORNO
                    )
                    lugares_guardar = lugares if (lugares or not error_lugares) else cache_previo.get("lugares", [])
                    aguas_guardar = aguas if (aguas or not error_aguas) else cache_previo.get("aguas", [])

                    if desplazamiento is not None and (lugares_guardar or aguas_guardar):
                        from django.utils import timezone as _tz
                        desplazamiento.cache_entorno = {
                            "lugares": lugares_guardar, "aguas": aguas_guardar,
                            "curvas": [list(c) for c in curvas], "riesgos": riesgos,
                            "completo": not (error_lugares or error_aguas) and bool(aguas_guardar),
                        }
                        desplazamiento.cache_entorno_actualizado = _tz.now()
                        desplazamiento.save(update_fields=["cache_entorno", "cache_entorno_actualizado"])

                if f_superficie is not None:
                    try:
                        auto_superficie = f_superficie.result()
                        if auto_superficie:
                            ruta_obj.tipo_superficie = list(auto_superficie.keys())
                            log.info("[EXCEL] tipo_superficie auto: %s", ruta_obj.tipo_superficie)
                    except Exception as e:
                        log.warning("[EXCEL] buscar_superficie_via falló: %s", e)

    perfiles = _perfiles_de_ruta(infos)
    criticos = _armar_criticos(curvas, riesgos, perfiles)
    zonas = _elegir_zonas(riesgos, lugares, perfiles)
    filas_apoyo = _armar_filas_apoyo(ruta_obj.puntos_apoyo_emergencia, riesgos, lugares, perfiles)

    _llenar_condiciones_via(ws, ruta_obj)

    if not (mapa_png_path and os.path.exists(mapa_png_path)) and infos:
        colegios_tabla = [p for clase, p in zonas if clase == "colegio"]
        riesgos_mapa = [p for p in riesgos
                        if p["tipo"] not in ESCOLARES and p["tipo"] != "roundabout"] + colegios_tabla
        ancho_px, alto_px = _tam_area_px(ws)
        mapa_png_path = generar_mapa_png_viajes(
            infos, curvas, riesgos_mapa,
            output_dir / f"Mapa_Ruta_{ruta_obj.id}.png",
            ancho=int(ancho_px * 1.5),
            alto=int(alto_px * 1.5),
            cache_dir=media_root / "cache_mosaicos",
            criticos=criticos,
        )

    if mapa_png_path and os.path.exists(mapa_png_path):
        img = Image(str(mapa_png_path))
        anclaje = TwoCellAnchor(editAs="twoCell")
        anclaje._from = AnchorMarker(
            col=MAPA_COL_INI - 1, row=MAPA_FILA_INI - 1, colOff=9525, rowOff=9525
        )
        anclaje.to = AnchorMarker(col=MAPA_COL_FIN, row=MAPA_FILA_FIN, colOff=0, rowOff=0)
        img.anchor = anclaje
        # Va PRIMERO en la lista para quedar debajo de la leyenda de la plantilla
        # (la leyenda de íconos se dibuja encima del mapa, arriba a la derecha).
        ws._images.insert(0, img)

    _llenar_tramos_criticos(ws, criticos, infos, ruta_obj)
    _llenar_zonas_urbanas(ws, zonas)
    _llenar_cuerpos_de_agua(ws, ruta_obj.cuerpos_de_agua, aguas)
    _llenar_puntos_apoyo(ws, filas_apoyo)

    anio_filtro = None
    if desplazamiento and getattr(desplazamiento, "fecha_desplazamiento", None):
        anio_filtro = desplazamiento.fecha_desplazamiento.year

        # _llenar_resumen_mensual(
    #     ws, ruta_obj, distancia_km=distancia_km,
    #     anio=anio_filtro,
    # )

    if avisos:
        c = Comment("AVISOS DEL SISTEMA\n" + "\n".join(avisos), "Sistema")
        c.width, c.height = 420, 180
        ws["A5"].comment = c

    # 11. GUARDAR (antes, se neutraliza cualquier texto externo que empiece por "=")
    _neutralizar_formulas(ws, formulas_legitimas)
    wb.save(salida_path)
    return salida_path


import time as _time


def _con_tiempo(func):
    def envoltura(*a, **kw):
        t0 = _time.perf_counter()
        try:
            return func(*a, **kw)
        finally:
            log.warning("[EXCEL] %s: %.1f s", func.__name__, _time.perf_counter() - t0)
    envoltura.__name__ = func.__name__
    return envoltura


datos_mapa_de_viajes = _con_tiempo(datos_mapa_de_viajes)
buscar_entorno_ruta = _con_tiempo(buscar_entorno_ruta)
buscar_superficie_via = _con_tiempo(buscar_superficie_via)
generar_mapa_png_viajes = _con_tiempo(generar_mapa_png_viajes)
_armar_filas_apoyo = _con_tiempo(_armar_filas_apoyo)
_llenar_tramos_criticos = _con_tiempo(_llenar_tramos_criticos)
_llenar_zonas_urbanas = _con_tiempo(_llenar_zonas_urbanas)
_llenar_cuerpos_de_agua = _con_tiempo(_llenar_cuerpos_de_agua)
_llenar_puntos_apoyo = _con_tiempo(_llenar_puntos_apoyo)
generar_excel_tarjeta_ruta = _con_tiempo(generar_excel_tarjeta_ruta)