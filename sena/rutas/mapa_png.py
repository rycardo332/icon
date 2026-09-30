"""
Imagen PNG del mapa para la Tarjeta de ruta (Excel).

Usa lo que ya calcula mapa_viajes.py (ruta OSRM guardada en Viaje, curvas cerradas
y puntos de riesgo), asi el mapa del Excel y el de la pagina salen de los mismos datos.

El mapa base (calles de OpenStreetMap) se arma aqui mismo con `requests` y Pillow;
ya no hace falta instalar `staticmap`. Si no hay internet, dibuja el recorrido sobre
fondo liso y lo avisa en el log (logger "rutas.mapa_png").

Que se dibuja:
- Triangulos amarillos numerados: los puntos criticos de la tabla "5. TRAMOS E
  INTERSECCIONES CRITICAS" (el numero es el mismo de la fila en el Excel).
- Circulos con letra: hospitales (H), bomberos (B), policia (P), Cruz Roja (C),
  zonas escolares (E), estaciones de servicio (G), peajes ($) y radares (R).
- Una leyenda abajo a la izquierda con lo que realmente aparece en el mapa.

- datos_mapa_de_viajes(viajes)   -> (infos, curvas, riesgos)
- generar_mapa_png_viajes(...)   -> guarda el PNG
"""
import io
import logging
import math
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

log = logging.getLogger(__name__)

USER_AGENT = "ICON-LTDA-TarjetaRuta/1.0 (uso interno)"
TILES_URL = "https://tile.openstreetmap.org/{z}/{x}/{y}.png"
TILE = 256

# tipo de OSM -> (color, letra dentro del circulo, texto de la leyenda)
ESTILO_RIESGO = {
    "hospital": ("#dc2626", "H", "Hospital / centro de salud"),
    "fire_station": ("#ea580c", "B", "Bomberos"),
    "police": ("#1d4ed8", "P", "Policía"),
    "red_cross": ("#e11d48", "C", "Cruz Roja"),
    "school": ("#0d9488", "E", "Zona escolar"),
    "college": ("#0d9488", "E", "Zona escolar"),
    "university": ("#0d9488", "E", "Zona escolar"),
    "fuel": ("#2563eb", "G", "Estación de servicio"),
    "toll_booth": ("#7e22ce", "$", "Peaje"),
    "speed_camera": ("#111827", "R", "Radar de velocidad"),
}
COLOR_CURVA = "#FFC400"         # Tramos e intersecciones criticas


def datos_mapa_de_viajes(viajes, con_riesgos=True, avisos=None):
    """Arma lo que necesita el mapa a partir de los Viaje.

    'avisos' (lista opcional): si Overpass no responde, aqui se agrega el motivo para que
    el Excel lo muestre en vez de salir vacio sin explicacion."""
    from .mapa_viajes import (ErrorOverpass, buscar_puntos_riesgo,
                              detectar_curvas_de_viajes, preparar_viaje)

    infos = []
    for n, v in enumerate(viajes, 1):
        info = preparar_viaje(v, n)
        if info:
            infos.append(info)

    _, curvas = detectar_curvas_de_viajes(infos)

    riesgos = []
    coords = [p for i in infos if i["ruta_real"] for p in i["geometria"]]
    if con_riesgos and coords:
        try:
            riesgos = buscar_puntos_riesgo(coords)
        except ErrorOverpass as e:
            log.warning("Sin puntos de apoyo/riesgo: Overpass no respondió (%s)", e)
            if avisos is not None:
                avisos.append("No se pudieron consultar hospitales, bomberos, policía, "
                              f"colegios ni estaciones (Overpass no respondió: {e}). "
                              "Vuelve a generar el Excel en unos minutos.")
    return infos, curvas, riesgos



def _fuente(tam):
    from PIL import ImageFont

    for nombre in ("DejaVuSans-Bold.ttf", "arialbd.ttf", "Arial Bold.ttf"):
        try:
            return ImageFont.truetype(nombre, tam)
        except OSError:
            continue
    try:
        return ImageFont.load_default(size=tam)
    except TypeError:
        return ImageFont.load_default()


def _mercator(lat, lon, z):
    """Coordenadas en pixeles del mundo (Web Mercator, como OpenStreetMap)."""
    n = TILE * (2 ** z)
    lat = max(min(lat, 85.0511), -85.0511)
    x = (lon + 180.0) / 360.0 * n
    y = (1 - math.log(math.tan(math.radians(lat)) + 1 / math.cos(math.radians(lat))) / math.pi) / 2 * n
    return x, y



def _bajar_mosaico(sesion, z, x, y, cache_dir):
    from PIL import Image

    if cache_dir:
        ruta = Path(cache_dir) / str(z) / str(x) / f"{y}.png"
        if ruta.exists():
            try:
                return Image.open(ruta).convert("RGB")
            except Exception:
                pass
    resp = sesion.get(TILES_URL.format(z=z, x=x, y=y), timeout=8)
    resp.raise_for_status()
    if cache_dir:
        ruta.parent.mkdir(parents=True, exist_ok=True)
        ruta.write_bytes(resp.content)
    return Image.open(io.BytesIO(resp.content)).convert("RGB")


def _base_con_mosaicos(puntos, ancho, alto, cache_dir=None):
    """Devuelve (imagen, proyector) con calles de OSM, o None si no se pudo."""
    try:
        import requests
        from PIL import Image
    except ImportError as e:
        log.warning("Mapa sin calles: falta una libreria (%s)", e)
        return None

    margen = int(min(ancho, alto) * 0.10)
    zoom = 3
    for z in range(17, 2, -1):
        xy = [_mercator(la, lo, z) for la, lo in puntos]
        if (max(p[0] for p in xy) - min(p[0] for p in xy) <= ancho - 2 * margen and
                max(p[1] for p in xy) - min(p[1] for p in xy) <= alto - 2 * margen):
            zoom = z
            break

    xy = [_mercator(la, lo, zoom) for la, lo in puntos]
    cx = (max(p[0] for p in xy) + min(p[0] for p in xy)) / 2
    cy = (max(p[1] for p in xy) + min(p[1] for p in xy)) / 2
    ox, oy = cx - ancho / 2, cy - alto / 2

    n = 2 ** zoom
    pedidos = [(tx % n, ty, tx, ty)
               for tx in range(math.floor(ox / TILE), math.floor((ox + ancho) / TILE) + 1)
               for ty in range(math.floor(oy / TILE), math.floor((oy + alto) / TILE) + 1)
               if 0 <= ty < n]

    sesion = requests.Session()
    sesion.headers["User-Agent"] = USER_AGENT
    errores = []

    def bajar(p):
        try:
            return p, _bajar_mosaico(sesion, zoom, p[0], p[1], cache_dir)
        except Exception as e:
            errores.append(str(e))
            return p, None

    with ThreadPoolExecutor(max_workers=8) as pool:
        resultados = list(pool.map(bajar, pedidos))

    ok = [(p, img) for p, img in resultados if img is not None]
    if not pedidos or len(ok) < 0.6 * len(pedidos):
        log.warning("Mapa sin calles: bajaron %d de %d mosaicos de OpenStreetMap. Primer error: %s",
                    len(ok), len(pedidos), errores[0] if errores else "-")
        return None

    lienzo = Image.new("RGB", (ancho, alto), "#e5e3df")
    for (_, ty, tx_real, _), img in ok:
        lienzo.paste(img, (int(tx_real * TILE - ox), int(ty * TILE - oy)))

    def proyector(lat, lon):
        x, y = _mercator(lat, lon, zoom)
        return x - ox, y - oy

    return lienzo, proyector


def _base_lisa(puntos, ancho, alto):
    """Fondo liso con cuadricula, proyeccion lineal (sin internet)."""
    from PIL import Image, ImageDraw

    img = Image.new("RGB", (ancho, alto), "#eef2f7")
    d = ImageDraw.Draw(img)
    for i in range(1, 10):
        d.line([(ancho * i / 10, 0), (ancho * i / 10, alto)], fill="#dde4ee", width=1)
        d.line([(0, alto * i / 10), (ancho, alto * i / 10)], fill="#dde4ee", width=1)

    lat0 = sum(p[0] for p in puntos) / len(puntos)
    kx = math.cos(math.radians(lat0))
    xs = [p[1] * kx for p in puntos]
    ys = [-p[0] for p in puntos]
    dx, dy = (max(xs) - min(xs)) or 1e-6, (max(ys) - min(ys)) or 1e-6
    margen = int(min(ancho, alto) * 0.12)
    esc = min((ancho - 2 * margen) / dx, (alto - 2 * margen) / dy)
    ox = (ancho - dx * esc) / 2 - min(xs) * esc
    oy = (alto - dy * esc) / 2 - min(ys) * esc

    def proyector(lat, lon):
        return lon * kx * esc + ox, -lat * esc + oy

    return img, proyector


def _centrado(d, x, y, texto, fuente, relleno, borde=None):
    """Escribe 'texto' centrado en (x, y) sin depender de 'anchor' (que falla con la
    fuente de respaldo de Pillow)."""
    x0, y0, x1, y1 = d.textbbox((0, 0), texto, font=fuente)
    kw = {"stroke_width": 2, "stroke_fill": borde} if borde else {}
    d.text((x - (x0 + x1) / 2, y - (y0 + y1) / 2), texto, fill=relleno, font=fuente, **kw)


def _circulo_letra(d, x, y, radio, color, letra, fuente):
    d.ellipse((x - radio - 3, y - radio - 3, x + radio + 3, y + radio + 3), fill="white")
    d.ellipse((x - radio, y - radio, x + radio, y + radio), fill=color)
    if letra:
        _centrado(d, x, y, letra, fuente, "white")


def _triangulo(d, x, y, r):
    d.polygon([(x, y - r - 3), (x - r - 3, y + r), (x + r + 3, y + r)],
              fill=COLOR_CURVA, outline="#333333")


def _leyenda(d, riesgo_pts, hay_criticos, alto, r):
    """Cuadro abajo a la izquierda, solo con lo que realmente esta dibujado."""
    entradas, vistos = [], set()
    if hay_criticos:
        entradas.append(("triangulo", None, None, "Tramo crítico (n.º = fila de la tabla 5)"))
    for tipo, _ in riesgo_pts:
        color, letra, texto = ESTILO_RIESGO[tipo]
        if texto not in vistos:
            vistos.add(texto)
            entradas.append(("circulo", color, letra, texto))
    if not entradas:
        return

    f = _fuente(max(12, alto // 42))
    f_letra = _fuente(max(10, int(r * 1.2)))
    alto_fila = max(20, int(r * 2.4))
    anchos = [d.textbbox((0, 0), e[3], font=f)[2] for e in entradas]
    ancho_caja = int(max(anchos) + alto_fila + 24)
    alto_caja = alto_fila * len(entradas) + 12
    x0, y1 = 10, alto - 10
    y0 = y1 - alto_caja
    d.rectangle((x0, y0, x0 + ancho_caja, y1), fill="white", outline="#9ca3af")

    for i, (forma, color, letra, texto) in enumerate(entradas):
        cy = y0 + 6 + alto_fila * i + alto_fila / 2
        cx = x0 + 8 + alto_fila / 2
        rr = alto_fila * 0.36
        if forma == "triangulo":
            d.polygon([(cx, cy - rr), (cx - rr, cy + rr * 0.8), (cx + rr, cy + rr * 0.8)],
                      fill=COLOR_CURVA, outline="#333333")
        else:
            d.ellipse((cx - rr, cy - rr, cx + rr, cy + rr), fill=color)
            _centrado(d, cx, cy, letra, f_letra, "white")
        _centrado(d, cx + alto_fila / 2 + 6 + anchos[i] / 2, cy, texto, f, "#111111")


def _dibujar(img, px, lineas, criticos, riesgo_pts, con_calles):
    from PIL import ImageDraw

    ancho, alto = img.size
    d = ImageDraw.Draw(img)
    g = max(4, alto // 90)

    for color, geom, _, _ in lineas:
        pts = [px(la, lo) for la, lo in geom]
        d.line(pts, fill="white", width=g + 6, joint="curve")
        d.line(pts, fill=color, width=g, joint="curve")

    r = max(8, alto // 55)
    f_letra = _fuente(max(10, int(r * 1.2)))

    def punto(p, color, radio):
        x, y = px(*p)
        d.ellipse((x - radio - 3, y - radio - 3, x + radio + 3, y + radio + 3), fill="white")
        d.ellipse((x - radio, y - radio, x + radio, y + radio), fill=color)

    # Hospitales, bomberos, policia, colegios, estaciones... (con su letra)
    for tipo, p in riesgo_pts:
        color, letra, _ = ESTILO_RIESGO[tipo]
        x, y = px(*p)
        _circulo_letra(d, x, y, r, color, letra, f_letra)

    # Triangulo amarillo + numero (el mismo que la fila de la tabla 5 del Excel)
    f_num = _fuente(max(16, alto // 26))
    for c in criticos:
        x, y = px(c["lat"], c["lon"])
        _triangulo(d, x, y, r)
        d.text((x + r + 6, y - r - 8), str(c["n"]), fill="#111111", font=f_num,
               stroke_width=3, stroke_fill="white")

    for _, _, o, dst in lineas:
        if o:
            punto(o, "#16a34a", r + 2)
        if dst:
            punto(dst, "#b00020", r + 2)

    _leyenda(d, riesgo_pts, bool(criticos), alto, r)

    texto = "© OpenStreetMap contributors" if con_calles else "Sin mapa base (sin conexión)"
    f = _fuente(max(11, alto // 45))
    x0, y0, x1, y1 = d.textbbox((0, 0), texto, font=f)
    if con_calles:
        d.rectangle((ancho - (x1 - x0) - 14, alto - (y1 - y0) - 12, ancho, alto), fill="white")
        d.text((ancho - (x1 - x0) - 8, alto - (y1 - y0) - 8), texto, fill="#333333", font=f)
    else:
        d.text((ancho - (x1 - x0) - 14, alto - (y1 - y0) - 12), texto, fill="#6b7280", font=f)


def generar_mapa_png_viajes(infos, curvas, riesgos, salida, ancho=1600, alto=700,
                            cache_dir=None, criticos=None):
    """Guarda el PNG en `salida`. Devuelve la ruta, o None si no hay ningun recorrido.

    criticos: lista de dicts {"n", "lat", "lon"} con los puntos de la tabla 5, ya numerados
    como las filas del Excel. Si no se da, se numeran todas las curvas (comportamiento
    anterior)."""
    lineas = [(i["color"], i["geometria"], i["origen"], i["destino"])
              for i in (infos or []) if len(i["geometria"]) > 1]
    if not lineas:
        return None

    if criticos is None:
        criticos = [{"n": n, "lat": c[2][0], "lon": c[2][1]} for n, c in enumerate(curvas or [], 1)]
    riesgo_pts = [(r["tipo"], (r["lat"], r["lon"]))
                  for r in (riesgos or []) if r["tipo"] in ESTILO_RIESGO]

    # El encuadre se ajusta a la ruta y a los puntos criticos; un hospital lejano
    # (hasta 5 km) puede quedar en el borde en vez de alejar el zoom para todo.
    puntos = [p for _, g, _, _ in lineas for p in g] + [(c["lat"], c["lon"]) for c in criticos]
    base = _base_con_mosaicos(puntos, ancho, alto, cache_dir)
    con_calles = base is not None
    img, px = base if con_calles else _base_lisa(puntos, ancho, alto)

    _dibujar(img, px, lineas, criticos, riesgo_pts, con_calles)
    img.save(salida, "PNG")
    return salida