"""Resumen del día: qué hizo cada vehículo (salidas desde Duitama), sin escribir rutas.

Formato real de FILPAC (verificado con el reporte HCQ911):
- 'Coordenadas' es "5.8286999 -73.0238616" (lat y lon separados por ESPACIO).
- 'Coordenadas de fin' repite la de inicio, así que el destino de una fila es el
  inicio de la fila siguiente (igual que hace importador_filpac.py).
- La dirección termina en el municipio: "..., SOGAMOSO, BOYACA" o "...,Duitama,Boyacá".
"""
from datetime import date
from math import asin, cos, radians, sin, sqrt

BASE = (5.8287, -73.0238)   # sede en Duitama (punto de partida real del GPS)
RADIO_BASE_KM = 3.0


def _km(a, b):
    la1, lo1, la2, lo2 = map(radians, (*a, *b))
    h = sin((la2 - la1) / 2) ** 2 + cos(la1) * cos(la2) * sin((lo2 - lo1) / 2) ** 2
    return 6371 * 2 * asin(sqrt(h))


def _coords(texto):
    try:
        lat, lon = texto.replace(",", " ").split()[:2]
        return float(lat), float(lon)
    except (ValueError, AttributeError):
        return None


def municipio(direccion):
    """'... KRA 11 CON CLL 28, SOGAMOSO, BOYACA' -> 'Sogamoso'."""
    partes = [p.strip() for p in (direccion or "").split(",") if p.strip()]
    while partes and partes[-1].upper().replace("Á", "A") == "BOYACA":
        partes.pop()
    if not partes:
        return ""
    ultimo = partes[-1]
    if ultimo.upper().endswith(" BOYACA"):        # "TIBASOSA BOYACA"
        ultimo = ultimo[: -len(" BOYACA")]
    return ultimo.title().strip()


def _en_base(coord):
    return coord is not None and _km(coord, BASE) <= RADIO_BASE_KM


def salidas_de_registros(regs):
    """regs: lista de dicts (inicio, fin, dir_inicio, dir_fin, coords_inicio, distancia)
    de UN vehículo en UN día, ordenados por inicio."""
    for i, r in enumerate(regs):
        r["_ini"] = _coords(r["coords_inicio"])
        sig = regs[i + 1] if i + 1 < len(regs) else None
        if sig:
            r["_dest"] = _coords(sig["coords_inicio"])
            r["_fin_en_base"] = _en_base(r["_dest"])
        else:  # última fila del día: no hay siguiente, se usa el municipio de su dirección
            r["_dest"] = None
            r["_fin_en_base"] = municipio(r["dir_fin"]) == "Duitama"

    salidas, actual = [], []
    for r in regs:
        if not actual and (not _en_base(r["_ini"]) or r["_fin_en_base"]):
            if not _en_base(r["_ini"]):          # el día empieza ya afuera
                actual.append(r)
            # (si empieza y termina en base: maniobra dentro de Duitama, se ignora)
            if actual and r["_fin_en_base"]:
                salidas.append((actual, True)); actual = []
            continue
        if not actual:
            actual.append(r)                      # primera fila que sale de la base
        else:
            actual.append(r)
        if r["_fin_en_base"]:
            salidas.append((actual, True)); actual = []
    if actual:
        salidas.append((actual, False))           # sigue afuera al terminar el día

    resultado = []
    for tramo, regreso in salidas:
        puntos = [(r["_ini"], municipio(r["dir_inicio"])) for r in tramo if r["_ini"]]
        if not puntos:
            continue
        coord_lejos, destino = max(puntos, key=lambda p: _km(p[0], BASE))
        if _km(coord_lejos, BASE) <= RADIO_BASE_KM:
            continue                              # nunca salió de Duitama
        paso, vistos = [], {"Duitama", destino}
        for _, m in puntos:
            if m and m not in vistos:
                paso.append(m); vistos.add(m)
        resultado.append({
            "origen": "Duitama",
            "destino": destino,
            "pasando_por": paso,
            "hora_salida": tramo[0]["inicio"],
            "hora_fin": tramo[-1]["fin"],
            "km_gps": round(sum(float(r["distancia"] or 0) for r in tramo), 1),
            "regreso_a_base": regreso,
            "lat_destino": coord_lejos[0],
            "lon_destino": coord_lejos[1],
            "km_hasta_destino": round(_km(coord_lejos, BASE), 1),
        })
    return resultado


def resumen_del_dia(fecha: date):
    from datetime import datetime, time, timedelta
    from django.conf import settings
    from django.utils import timezone
    from .models import RegistroGPS, Vehiculo   # import local: evita ciclos

    # Rango en vez de `__date`: en MySQL `__date` devuelve vacío sin tablas de zona horaria
    ini = datetime.combine(fecha, time.min)
    if settings.USE_TZ:
        ini = timezone.make_aware(ini)
    fin = ini + timedelta(days=1)

    resultado = []
    objetos = (RegistroGPS.objects.filter(inicio__gte=ini, inicio__lt=fin)
               .values_list("objeto_gps", flat=True).distinct())
    for objeto in objetos:
        veh = (Vehiculo.objects.filter(id_objeto_gps=objeto).first()
               or Vehiculo.objects.filter(placa=objeto).first())
        regs = [{
            "inicio": r.inicio, "fin": r.fin,
            "dir_inicio": r.direccion_inicio, "dir_fin": r.direccion_fin,
            "coords_inicio": r.coordenadas_inicio, "distancia": r.distancia,
        } for r in RegistroGPS.objects.filter(objeto_gps=objeto, inicio__gte=ini, inicio__lt=fin).order_by("inicio")]
        for s in salidas_de_registros(regs):
            s["placa"] = veh.placa if veh else objeto
            s["vehiculo"] = veh
            resultado.append(s)
    return resultado