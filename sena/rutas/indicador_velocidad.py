"""Indicador PESV de excesos de velocidad laboral (SIG.FT-57).

Calculo (basado en lo que guarda el importador de FILPAC):
  - Km totales del mes  = suma de RegistroGPS.distancia (incluye maniobras menores).
  - Km con exceso       = suma de RegistroGPS.distancia de los registros cuya
                          velocidad_maxima supera el limite.
FILPAC solo da la velocidad MAXIMA de cada trayecto, asi que un trayecto con un solo
pico por encima del limite cuenta completo. Es el mismo criterio que se dedujo al
comparar con la plantilla de referencia.

Configuracion opcional en settings.py:
    PESV_LIMITE_VELOCIDAD = 80          # km/h
    PESV_ANIO_INICIAL = 2026            # primer anio que aparece en el selector
    PESV_PLANTILLA_VELOCIDAD = BASE_DIR / "rutas/plantillas_excel/INDICADOR_PESV_VELOCIDAD.xlsx"
"""
import os
import zipfile
from collections import defaultdict
from datetime import datetime

from django.conf import settings
from django.utils import timezone
from lxml import etree
from openpyxl.utils.cell import column_index_from_string, coordinate_from_string

META = 0.05  # 5 %
ANIO_INICIAL = getattr(settings, "PESV_ANIO_INICIAL", 2026)  # primer anio del indicador
FILA_ENERO = 10  # en cada hoja de la plantilla, enero es la fila 10 y diciembre la 21
MESES = ["Enero", "Febrero", "Marzo", "Abril", "Mayo", "Junio", "Julio",
         "Agosto", "Septiembre", "Octubre", "Noviembre", "Diciembre"]

NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
RNS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"


def limite_por_defecto():
    return getattr(settings, "PESV_LIMITE_VELOCIDAD", 80)


def _normalizar(placa):
    """'GSM 875' y 'gsm875' se tratan como la misma placa."""
    return "".join(str(placa).split()).upper()


def calcular_indicadores(anio, limite=None):
    """Devuelve {placa: {mes(1-12): {"total": km, "exceso": km}}} solo con meses que tienen registros."""
    from .models import RegistroGPS

    limite = limite_por_defecto() if limite is None else limite
    desde = timezone.make_aware(datetime(anio, 1, 1))
    hasta = timezone.make_aware(datetime(anio + 1, 1, 1))

    resultado = defaultdict(lambda: defaultdict(lambda: {"total": 0.0, "exceso": 0.0}))
    filas = RegistroGPS.objects.filter(inicio__gte=desde, inicio__lt=hasta).values_list(
        "objeto_gps", "inicio", "distancia", "velocidad_maxima"
    )
    # El mes se saca en Python (hora de Bogota) para no depender de las tablas de
    # zona horaria de MySQL.
    for objeto, inicio, distancia, vmax in filas.iterator():
        if distancia is None:
            continue
        celda = resultado[_normalizar(objeto)][timezone.localtime(inicio).month]
        celda["total"] += float(distancia)
        if vmax is not None and vmax > limite:
            celda["exceso"] += float(distancia)
    return {p: dict(m) for p, m in resultado.items()}


def resumen_para_pagina(datos):
    """Agrega el % mensual y anual por vehiculo, listo para pintar en la pagina."""
    salida = {}
    for placa, meses in datos.items():
        filas = []
        for n in range(1, 13):
            m = meses.get(n)
            pct = (m["exceso"] / m["total"]) if m and m["total"] else None
            filas.append({"mes": MESES[n - 1], "total": m["total"] if m else None,
                          "exceso": m["exceso"] if m else None, "pct": pct,
                          "fuente": m.get("fuente") if m else None,
                          "cumple": (pct <= META) if pct is not None else None})
        total = sum(m["total"] for m in meses.values())
        exceso = sum(m["exceso"] for m in meses.values())
        salida[placa] = {"meses": filas, "total": total, "exceso": exceso,
                         "pct_anual": (exceso / total) if total else None}
    return salida


def leer_plantilla(anio, plantilla=None):
    """Lee lo que ya trae la plantilla de Excel (columnas C y D de cada hoja de vehiculo).

    Devuelve {placa: {mes(1-12): {"total": km, "exceso": km, "fuente": "plantilla"}}}.
    Solo si la plantilla es del mismo anio que se pide; si no, devuelve {}.
    """
    plantilla = plantilla or getattr(settings, "PESV_PLANTILLA_VELOCIDAD", None)
    if not plantilla or not os.path.exists(plantilla):
        return {}

    salida = {}
    with zipfile.ZipFile(plantilla) as z:
        hojas = _hojas(z)
        textos = _textos_compartidos(z)
        for nombre, ruta in hojas.items():
            raiz = etree.fromstring(z.read(ruta))
            sd = raiz.find(_q("sheetData"))
            if _valor(_celda(sd, "B", FILA_ENERO), textos) != "ENERO":
                continue  # no es hoja de vehiculo
            try:
                if int(float(_valor(_celda(sd, "I", 6), textos))) != anio:
                    continue  # la plantilla es de otro anio
            except (TypeError, ValueError):
                continue

            meses = {}
            for n in range(1, 13):
                fila = FILA_ENERO + n - 1
                try:
                    total = float(_valor(_celda(sd, "C", fila), textos))
                except (TypeError, ValueError):
                    continue  # mes vacio en la plantilla
                try:
                    exceso = float(_valor(_celda(sd, "D", fila), textos) or 0)
                except (TypeError, ValueError):
                    exceso = 0.0
                meses[n] = {"total": total, "exceso": exceso, "fuente": "plantilla"}
            if meses:
                salida[_normalizar(nombre)] = meses
    return salida


def completar_con_plantilla(datos, anio, plantilla=None):
    """Mezcla lo calculado desde el GPS con lo que trae la plantilla.

    Si un mes tiene registros en la BD, manda la BD (fuente "gps").
    Si no tiene, se usa el valor de la plantilla (fuente "plantilla").

    Al final deja solo los vehiculos registrados en el modulo Vehiculos.
    """
    resultado = {}
    for placa, meses in datos.items():
        resultado[placa] = {n: {**m, "fuente": "gps"} for n, m in meses.items()}
    for placa, meses in leer_plantilla(anio, plantilla).items():
        destino = resultado.setdefault(placa, {})
        for n, m in meses.items():
            destino.setdefault(n, m)
    return solo_vehiculos_registrados(resultado)


def solo_vehiculos_registrados(datos):
    """Deja en el indicador unicamente los vehiculos ACTIVOS del modulo Vehiculos.

    - Las placas que estan en la plantilla/GPS pero NO en Vehiculos no se muestran
      (sus datos siguen guardados; aparecen solos cuando se registre esa placa).
    - Los vehiculos desactivados tampoco se muestran; al reactivarlos vuelven a
      aparecer con todo su historial.
    - Los vehiculos activos que aun no tienen datos aparecen con meses vacios.
    """
    from .models import Vehiculo

    registradas = {
        _normalizar(p)
        for p in Vehiculo.objects.filter(activo=True).values_list("placa", flat=True)
    }
    return {placa: datos.get(placa, {}) for placa in registradas}


# ----------------------------------------------------------------------
# Edicion directa del XML del .xlsx (no pasa por openpyxl, asi las graficas,
# el logo y los estilos de la plantilla quedan intactos)
# ----------------------------------------------------------------------

def _q(t):
    return f"{{{NS}}}{t}"


def _hojas(z):
    """{nombre_hoja: ruta_dentro_del_zip}"""
    wb = etree.fromstring(z.read("xl/workbook.xml"))
    rels = etree.fromstring(z.read("xl/_rels/workbook.xml.rels"))
    destino = {r.get("Id"): r.get("Target") for r in rels}
    salida = {}
    for s in wb.find(_q("sheets")):
        t = destino[s.get(f"{{{RNS}}}id")]
        salida[s.get("name")] = t.lstrip("/") if t.startswith("/xl") else "xl/" + t
    return salida


def _textos_compartidos(z):
    if "xl/sharedStrings.xml" not in z.namelist():
        return []
    raiz = etree.fromstring(z.read("xl/sharedStrings.xml"))
    return ["".join(t.text or "" for t in si.iter(_q("t"))) for si in raiz]


def _celda(sheetdata, col, fila):
    """Devuelve la celda (la crea en su sitio si no existe)."""
    ref = f"{col}{fila}"
    row = next((r for r in sheetdata if r.get("r") == str(fila)), None)
    if row is None:
        row = etree.SubElement(sheetdata, _q("row"), r=str(fila))
    idx = column_index_from_string(col)
    previa = None
    for c in row:
        if c.get("r") == ref:
            return c
        if column_index_from_string(coordinate_from_string(c.get("r"))[0]) < idx:
            previa = c
    nueva = etree.Element(_q("c"), r=ref)
    if previa is None:
        row.insert(0, nueva)
    else:
        previa.addnext(nueva)
    return nueva


def _valor(celda, textos):
    v = celda.find(_q("v"))
    if v is None or v.text is None:
        return None
    if celda.get("t") == "s":
        return textos[int(v.text)]
    return v.text


def _poner(celda, numero):
    """Escribe un numero (o deja la celda vacia si es None), conservando su estilo."""
    for hijo in list(celda):
        celda.remove(hijo)
    celda.attrib.pop("t", None)
    if numero is not None:
        etree.SubElement(celda, _q("v")).text = repr(float(numero))


def _a_bytes(raiz):
    return etree.tostring(raiz, xml_declaration=True, encoding="UTF-8", standalone=True)


def generar_excel(datos, anio, salida, plantilla=None, conservar_existentes=True):
    """Copia la plantilla editando SOLO las celdas C y D de cada hoja de vehiculo.

    Las formulas (% mensual, acumulados, % anual, Hoja6), las graficas y el logo
    quedan como estaban; Excel recalcula todo al abrir el archivo.

    Con conservar_existentes=True (por defecto), los meses SIN registros en la BD
    mantienen lo que ya traia la plantilla (por ejemplo, enero-julio digitados a mano).
    Solo se hace si la plantilla es del mismo anio que se pide; si se pide otro anio
    (p. ej. 2027 sobre una plantilla 2026) la tabla se limpia para no mezclar anios.

    Nota: solo escribe en las hojas que ya existen en la plantilla (una por
    vehiculo). Un vehiculo nuevo necesita su propia hoja en el .xlsx.
    """
    plantilla = plantilla or getattr(settings, "PESV_PLANTILLA_VELOCIDAD")

    with zipfile.ZipFile(plantilla) as zin:
        hojas = _hojas(zin)
        textos = _textos_compartidos(zin)
        nuevos = {}  # ruta dentro del zip -> bytes modificados

        for nombre, ruta in hojas.items():
            raiz = etree.fromstring(zin.read(ruta))
            sd = raiz.find(_q("sheetData"))
            if _valor(_celda(sd, "B", FILA_ENERO), textos) != "ENERO":
                continue  # no es hoja de vehiculo (Hoja6, etc.)

            i6 = _celda(sd, "I", 6)
            try:
                mismo_anio = int(float(_valor(i6, textos))) == anio
            except (TypeError, ValueError):
                mismo_anio = False
            conservar = conservar_existentes and mismo_anio
            _poner(i6, anio)

            meses = datos.get(_normalizar(nombre), {})
            for n in range(1, 13):
                fila = FILA_ENERO + n - 1
                m = meses.get(n)
                if m is None and conservar:
                    continue  # mes sin datos en la BD: se deja lo que trae la plantilla
                _poner(_celda(sd, "C", fila), round(m["total"], 2) if m else None)
                _poner(_celda(sd, "D", fila), round(m["exceso"], 2) if m else None)

            # Quita los resultados viejos de las formulas; Excel los recalcula al abrir.
            for c in sd.iter(_q("c")):
                if c.find(_q("f")) is not None:
                    v = c.find(_q("v"))
                    if v is not None:
                        c.remove(v)
                    c.attrib.pop("t", None)
            nuevos[ruta] = _a_bytes(raiz)

        # Recalcular todo al abrir
        wb = etree.fromstring(zin.read("xl/workbook.xml"))
        calc = wb.find(_q("calcPr"))
        if calc is None:
            calc = etree.SubElement(wb, _q("calcPr"))
        calc.set("fullCalcOnLoad", "1")
        nuevos["xl/workbook.xml"] = _a_bytes(wb)

        # Quitar calcChain.xml y sus referencias (causa comun del mensaje "Reparado")
        if "[Content_Types].xml" in zin.namelist():
            ct = etree.fromstring(zin.read("[Content_Types].xml"))
            for o in list(ct):
                if o.get("PartName") == "/xl/calcChain.xml":
                    ct.remove(o)
            nuevos["[Content_Types].xml"] = _a_bytes(ct)

        rels = etree.fromstring(zin.read("xl/_rels/workbook.xml.rels"))
        for r in list(rels):
            if r.get("Target", "").endswith("calcChain.xml"):
                rels.remove(r)
        nuevos["xl/_rels/workbook.xml.rels"] = _a_bytes(rels)

        with zipfile.ZipFile(salida, "w", zipfile.ZIP_DEFLATED) as zout:
            for item in zin.infolist():
                if item.filename == "xl/calcChain.xml":
                    continue
                zout.writestr(item, nuevos.get(item.filename, zin.read(item.filename)))
    return salida