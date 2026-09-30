"""Genera el Word "CONTROL DE PLANIFICACIÓN DE DESPLAZAMIENTOS LABORALES" (SIG-FT-77, v02).

Igual que con el Excel, se parte del formato original de la empresa (plantilla .docx) y solo
se reemplazan los datos: encabezado, logo, códigos, control de cambios y aprobaciones se
conservan intactos. Los textos fijos que el formato vacío trae en blanco (equipo adicional,
acción preventiva, límites de velocidad) están como constantes arriba y se pueden editar.

Uso:
    from .generador_word import generar_word_control_planificacion
    ruta_docx = generar_word_control_planificacion(desplazamiento)

Requiere:  pip install python-docx
Plantilla: <BASE_DIR>/plantillas/plantilla_control_planificacion.docx

Cada tabla y cada fila se busca por su TÍTULO / ETIQUETA (no por posición), así que si la
empresa reordena filas o agrega texto, sigue funcionando. Si no encuentra una tabla lanza un
ValueError que dice cuál falta.

Qué se llena y de dónde sale:
  INFORMACIÓN GENERAL      -> Conductor, Desplazamiento (fecha y horas)
  RUTA Y DESTINO           -> pasos de OSRM sobre los Viaje GPS del día (o, si no hay,
                              Ruta.instrucciones_recorrido / descripcion_ruta) y Ruta.destino
  VEHÍCULO                 -> Vehiculo.placa + Vehiculo.tipo
  INSPECCIÓN (mecánica)    -> InspeccionVehiculo/ItemInspeccion (solo si ya se hizo la inspección)
  INSPECCIÓN (documentos)  -> se calcula con las fechas de vencimiento vs. la fecha del desplazamiento
  RIESGOS                  -> textos base + condiciones de la Ruta + RutaRiesgo
  PUNTOS CRÍTICOS          -> PuntoRuta de la ruta (punto crítico / intersección / zona escolar)
  DESCANSO / CLIMA / ALCOHOLEMIA / PARADAS -> campos del Desplazamiento
  EQUIPO / ACCIÓN PREVENTIVA / LÍMITES DE VELOCIDAD -> textos estándar (constantes al inicio)
  RESPONSABLE              -> Desplazamiento.planificado_por
"""
import logging
import os
import re
import unicodedata
from copy import deepcopy
from pathlib import Path

import docx
from docx.oxml import OxmlElement, parse_xml
from docx.oxml.ns import qn

log = logging.getLogger(__name__)

MESES = ["enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto",
         "septiembre", "octubre", "noviembre", "diciembre"]

W14 = "{http://schemas.microsoft.com/office/word/2010/wordml}"


RIESGOS_BASE = [
    ("Lluvia y calzada húmeda:",
     " disminución de la adherencia, aumento de la distancia de frenado y posibilidad de deslizamiento."),
    ("Ingreso y salida de zonas urbanas:",
     " mayor presencia de peatones, motociclistas, ciclistas y vehículos que realizan maniobras inesperadas."),
    ("Condiciones climáticas variables:",
     " en Boyacá las condiciones pueden cambiar rápidamente durante el recorrido."),
    ("Fatiga o distracción del conductor:",
     " se debe evitar el uso del celular y cualquier actividad que reduzca la concentración."),
]
RIESGOS_POR_VISIBILIDAD = {
    "niebla": ("Neblina:", " posible reducción considerable de la visibilidad, especialmente en sectores "
                           "rurales y de montaña."),
    "curvas": ("Curvas y cambios de pendiente:", " la ruta presenta tramos con curvas, ascensos y descensos "
                                                 "que requieren reducción de velocidad y mayor atención."),
    "material_particulado": ("Material particulado suspendido:", " polvo o humo que reduce la visibilidad; "
                                                                 "aumentar la distancia de seguimiento."),
    "iluminacion": ("Iluminación deficiente:", " tramos con poca luz que exigen reducir la velocidad y "
                                               "usar las luces adecuadamente."),
}
RIESGO_TRAFICO_PESADO = ("Vehículos pesados:", " puede existir circulación de camiones y otros vehículos "
                                               "de carga.")
RIESGO_VIA_RURAL = ("Vías secundarias rurales:", " posibilidad de encontrar estrechamientos, irregularidades "
                                                 "en la vía, gravilla, barro, animales o peatones.")
RIESGOS_POR_CONDICION = {
    "inestabilidad": ("Inestabilidad geológica:", " posibles derrumbes o caída de material sobre la vía."),
    "caida_bancada": ("Caída de bancada:", " posible pérdida parcial de la calzada; extremar precaución."),
    "hundimientos": ("Hundimientos:", " deformaciones en la calzada que exigen reducir la velocidad."),
    "zona_inundable": ("Zona inundable:", " posible acumulación de agua sobre la vía en época de lluvias."),
}


LIMITES_VELOCIDAD = [
    ("Vías urbanas y carreteras municipales:", " velocidad máxima: 50 km/h."),
    ("Vías nacionales y departamentales:", " velocidad máxima: 90 km/h."),
    ("Vías rurales:", " velocidad máxima: 80 km/h."),
    ("Zonas escolares y residenciales:", " velocidad máxima: 30 km/h."),
]


EQUIPO_ADICIONAL = [
    "Botiquín de primeros auxilios.",
    "Extintor vigente.",
    "Kit de carretera y elementos de señalización.",
    "Llanta de repuesto y herramientas para cambio.",
    "Equipo de comunicación: teléfono celular cargado.",
    "Radio de comunicación, cuando aplique.",
    "Herramientas básicas (alicates, destornilladores)",
    "Agua potable",
    "Contactos de emergencia.",
]
ACCION_PREVENTIVA = [
    ("Conservar la calma y detener el vehículo en un lugar seguro,", " siempre que las condiciones lo permitan."),
    ("Activar las luces estacionarias.", ""),
    ("Utilizar el chaleco reflectivo", " antes de descender del vehículo."),
    ("Señalizar la zona", " utilizando los dispositivos reglamentarios disponibles en el equipo de carretera."),
    ("Evaluar el estado de los ocupantes", " y prestar primeros auxilios básicos únicamente si se cuenta con las "
                                          "condiciones y conocimientos necesarios."),
    ("No mover personas lesionadas,", " salvo que exista un peligro inminente."),
    ("Informar inmediatamente a los organismos de emergencia y a la empresa ICON LTDA.", ""),
    ("", "En caso de falla mecánica, evitar reparaciones sobre la vía cuando exista riesgo por el tránsito."),
    ("Si existen derrames, riesgo eléctrico, incendio o materiales peligrosos:",
     " alejarse del área y evitar fuentes de ignición."),
    ("", "Registrar y reportar el evento conforme al procedimiento interno de seguridad vial de la empresa."),
]


CLAVES_MECANICAS = {
    "neumatic": ("neumatic", "llanta"),
    "luces": ("luces", "luz "),
    "freno": ("freno",),
    "direccion": ("direccion",),
    "liquido": ("liquido", "aceite", "refrigerante"),
}


def _norm(texto):
    """Minúsculas, sin tildes y con espacios simples, para comparar etiquetas."""
    t = unicodedata.normalize("NFD", str(texto or ""))
    t = "".join(c for c in t if unicodedata.category(c) != "Mn").lower()
    return re.sub(r"\s+", " ", t).strip()


def _fecha_larga(f):
    """date -> '01 de Julio de 2026' (como en el formato)."""
    if not f:
        return ""
    return f"{f.day:02d} de {MESES[f.month - 1].capitalize()} de {f.year}"


def _hora(t):
    """time -> '6:47 am' (como en el formato)."""
    if not t:
        return ""
    return f"{t.hour % 12 or 12}:{t.minute:02d} {'am' if t.hour < 12 else 'pm'}"


def _fecha_hora(dt):
    """datetime -> '01/07/2026 6:30 am' (hora local si tiene zona horaria)."""
    if not dt:
        return ""
    try:
        from django.utils import timezone
        if timezone.is_aware(dt):
            dt = timezone.localtime(dt)
    except Exception:
        pass
    return f"{dt:%d/%m/%Y} {_hora(dt.time())}"


def _lineas(texto):
    return [l.strip() for l in str(texto or "").splitlines() if l.strip()]



def _sin_ids(p):
    """Quita los w14:paraId/textId de un párrafo copiado (no deben repetirse)."""
    for atributo in ("paraId", "textId"):
        p.attrib.pop(W14 + atributo, None)


def _quitar_runs(p):
    for hijo in list(p):
        if hijo.tag != qn("w:pPr"):
            p.remove(hijo)


def _rpr_de_parrafo(p):
    """Formato del texto de la celda: el que trae la marca de párrafo, sin negrita."""
    ppr = p.find(qn("w:pPr"))
    rpr = ppr.find(qn("w:rPr")) if ppr is not None else None
    if rpr is None:
        return None
    rpr = deepcopy(rpr)
    _sin_negrita(rpr)
    return rpr


def _sin_negrita(rpr):
    for tag in ("w:b", "w:bCs"):
        for e in rpr.findall(qn(tag)):
            rpr.remove(e)


def _con_negrita(rpr):
    rpr = deepcopy(rpr) if rpr is not None else OxmlElement("w:rPr")
    _sin_negrita(rpr)
    pos = 1 if rpr.find(qn("w:rFonts")) is not None else 0
    rpr.insert(pos, OxmlElement("w:b"))
    rpr.insert(pos + 1, OxmlElement("w:bCs"))
    return rpr


def _nuevo_run(texto, rpr):
    r = OxmlElement("w:r")
    if rpr is not None:
        r.append(deepcopy(rpr))
    t = OxmlElement("w:t")
    t.set(qn("xml:space"), "preserve")
    t.text = texto
    r.append(t)
    return r


def _escribir_celda(celda, lineas):
    """Reemplaza el contenido de la celda: un párrafo por línea, con el formato de la celda.
    Con lista vacía deja la celda en blanco."""
    tc = celda._tc
    parrafos = tc.findall(qn("w:p"))
    base = parrafos[0]
    for p in parrafos[1:]:
        tc.remove(p)
    _quitar_runs(base)
    rpr = _rpr_de_parrafo(base)

    lineas = list(lineas) or [""]
    todos = [base]
    for _ in lineas[1:]:
        nuevo = deepcopy(base)
        _sin_ids(nuevo)
        tc.append(nuevo)
        todos.append(nuevo)
    for p, texto in zip(todos, lineas):
        if texto:
            p.append(_nuevo_run(texto, rpr))


def _numid_vineta(doc):
    """Crea (una vez) una lista con viñeta • en la numeración del documento y devuelve su numId.
    El formato vacío solo trae listas numeradas (1., 2.), por eso se define aquí."""
    numbering = doc.part.numbering_part.element
    abs_id = max([int(a.get(qn("w:abstractNumId"))) for a in numbering.findall(qn("w:abstractNum"))],
                 default=-1) + 1
    num_id = max([int(n.get(qn("w:numId"))) for n in numbering.findall(qn("w:num"))], default=0) + 1
    ns = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
    abstracto = parse_xml(
        f'<w:abstractNum {ns} w:abstractNumId="{abs_id}"><w:multiLevelType w:val="hybridMultilevel"/>'
        '<w:lvl w:ilvl="0"><w:start w:val="1"/><w:numFmt w:val="bullet"/><w:lvlText w:val="\u2022"/>'
        '<w:lvlJc w:val="left"/><w:pPr><w:ind w:left="360" w:hanging="360"/></w:pPr>'
        '<w:rPr><w:rFonts w:ascii="Arial" w:hAnsi="Arial" w:cs="Arial"/></w:rPr></w:lvl></w:abstractNum>')
    primer_num = numbering.find(qn("w:num"))  
    if primer_num is not None:
        primer_num.addprevious(abstracto)
    else:
        numbering.append(abstracto)
    numbering.append(parse_xml(
        f'<w:num {ns} w:numId="{num_id}"><w:abstractNumId w:val="{abs_id}"/></w:num>'))
    return num_id


def _escribir_vinetas(celda, items, numid=None):
    """Reemplaza el contenido de una celda con viñetas [(título_en_negrita, texto), ...],
    clonando el primer párrafo de la celda y poniéndole la viñeta `numid`."""
    tc = celda._tc
    parrafos = tc.findall(qn("w:p"))
    modelo = deepcopy(parrafos[0])
    runs = modelo.findall(qn("w:r"))
    rpr_base = None
    if runs and runs[0].find(qn("w:rPr")) is not None:
        rpr_base = runs[0].find(qn("w:rPr"))
    elif modelo.find(qn("w:pPr")) is not None:
        rpr_base = modelo.find(qn("w:pPr")).find(qn("w:rPr"))
    rpr_neg = _con_negrita(rpr_base)
    rpr_norm = deepcopy(rpr_base) if rpr_base is not None else None
    if rpr_norm is not None:
        _sin_negrita(rpr_norm)
    _quitar_runs(modelo)

    if numid is not None:
        ppr = modelo.find(qn("w:pPr"))
        if ppr is None:
            ppr = OxmlElement("w:pPr")
            modelo.insert(0, ppr)
        for viejo in ppr.findall(qn("w:numPr")):
            ppr.remove(viejo)
        ppr.insert(1 if ppr.find(qn("w:pStyle")) is not None else 0, parse_xml(
            '<w:numPr xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
            f'<w:ilvl w:val="0"/><w:numId w:val="{numid}"/></w:numPr>'))

    for p in parrafos:
        tc.remove(p)
    for titulo, texto in items:
        p = deepcopy(modelo)
        _sin_ids(p)
        if titulo:
            p.append(_nuevo_run(titulo, rpr_neg))
        if texto:
            p.append(_nuevo_run(texto, rpr_norm))
        tc.append(p)


def _reemplazar_texto_de_parrafo(p, texto):
    """Cambia el texto de un párrafo conservando su formato (alineación, fuente)."""
    rpr = _rpr_de_parrafo(p)
    _quitar_runs(p)
    if texto:
        p.append(_nuevo_run(texto, rpr))


def _agregar_texto(celda, texto):
    """Agrega texto al final del primer párrafo de la celda (sin negrita)."""
    p = celda._tc.findall(qn("w:p"))[0]
    runs = p.findall(qn("w:r"))
    rpr = None
    if runs and runs[-1].find(qn("w:rPr")) is not None:
        rpr = deepcopy(runs[-1].find(qn("w:rPr")))
        _sin_negrita(rpr)
    else:
        rpr = _rpr_de_parrafo(p)
    p.append(_nuevo_run(texto, rpr))



def _tabla(doc, titulo):
    clave = _norm(titulo)
    for t in doc.tables:
        if t.rows and clave in _norm(t.rows[0].cells[0].text):
            return t
    raise ValueError(f"No encontré la tabla '{titulo}' en la plantilla Word.")


def _fila(tabla, etiqueta, despues=False, col_etiqueta=0):
    """Fila cuya celda `col_etiqueta` contiene `etiqueta`; con despues=True, la fila siguiente.
    Se salta la fila 0 (el título de la tabla) para que una etiqueta no coincida con él."""
    clave = _norm(etiqueta)
    for i, fila in enumerate(tabla.rows):
        if i == 0:
            continue
        if clave in _norm(fila.cells[col_etiqueta].text):
            j = i + 1 if despues else i
            if j < len(tabla.rows):
                return tabla.rows[j]
    raise ValueError(f"No encontré la fila '{etiqueta}' en la plantilla Word.")



def _vigente(hasta, fecha):
    """'X' si el documento sigue vigente el día del desplazamiento, 'NO' si ya venció."""
    if not hasta or not fecha:
        return ""
    return "X" if hasta >= fecha else "NO"


def _riesgos_de_ruta(ruta):
    items = list(RIESGOS_BASE)

    for valor in (ruta.visibilidad or []):
        if valor in RIESGOS_POR_VISIBILIDAD:
            items.append(RIESGOS_POR_VISIBILIDAD[valor])
    if ruta.trafico in ("camiones", "mixto"):
        items.append(RIESGO_TRAFICO_PESADO)
    if set(ruta.tipo_superficie or []) & {"trocha", "con_huecos", "angosta"}:
        items.append(RIESGO_VIA_RURAL)
    for valor in (ruta.condiciones_generales or []):
        if valor in RIESGOS_POR_CONDICION:
            items.append(RIESGOS_POR_CONDICION[valor])

    
    try:
        for rr in ruta.riesgos.select_related("riesgo").all():
            medida = rr.medida_preventiva
            items.append((f"{rr.riesgo.nombre}:", f" {medida}" if medida else ""))
    except Exception:
        pass
    return items


def _puntos_criticos(ruta):
    try:
        puntos = ruta.puntos.filter(
            tipo__in=["punto_critico", "interseccion", "zona_escolar"]
        ).order_by("orden")
        salida = []
        for p in puntos:
            linea = p.nombre
            if p.descripcion:
                linea += f": {p.descripcion}"
            if p.referencia_km:
                linea += f" (km {p.referencia_km})"
            salida.append(linea)
        return salida
    except Exception:
        return []


def _clave_mecanica(nombre):
    n = _norm(nombre) + " "
    for clave, palabras in CLAVES_MECANICAS.items():
        if any(p in n for p in palabras):
            return clave
    return None


def _inspeccion(desplazamiento):
    """({clave: 'X'|'n/a'|''}, texto_para_'Otros') a partir de la inspección presencial, si existe."""
    marcas, otros = {}, []
    insp = getattr(desplazamiento, "inspeccion", None)  
    if insp is None:
        return marcas, ""
    for it in insp.items.select_related("item").all():
        clave = _clave_mecanica(it.item.nombre)
        if it.estado == "cumple":
            if clave:
                marcas[clave] = "X"
        elif it.estado == "no_aplica":
            if clave:
                marcas[clave] = "n/a"
        else:  # no cumple
            detalle = f"{it.item.nombre}: no cumple"
            if it.observacion:
                detalle += f" ({it.observacion})"
            otros.append(detalle)
    if insp.observaciones:
        otros.append(insp.observaciones)
    return marcas, "; ".join(otros)


def _ruta_desde_gps(viajes):
    """Instrucciones paso a paso a partir de los viajes GPS del día (ver instrucciones_ruta.py).
    Si OSRM no responde o no hay coordenadas, devuelve [] y se usa el texto de la Ruta."""
    if not viajes:
        return []
    try:
        from . import instrucciones_ruta
        return instrucciones_ruta.lineas_de_ruta(viajes)
    except Exception:
        log.exception("No se pudieron armar las instrucciones de ruta desde el GPS")
        return []


def armar_datos(d, viajes=None):
    """Convierte un Desplazamiento en el diccionario de textos que se escriben en el Word.
    `viajes`: los Viaje GPS del día (opcional); de ahí sale la ruta a seguir paso a paso."""
    ruta, veh, cond = d.ruta, d.vehiculo, d.conductor
    fecha = d.fecha_desplazamiento

    
    ruta_lineas = (_ruta_desde_gps(viajes) or _lineas(ruta.instrucciones_recorrido)
                   or _lineas(ruta.descripcion_ruta))
    if not ruta_lineas:
        ruta_lineas = [f"{ruta.origen} → {ruta.destino}"]

    marcas, otros = _inspeccion(d)
    marcas.update({
        "soat": _vigente(veh.soat_vigente_hasta, fecha),
        "revision": _vigente(veh.revision_tecnomecanica_hasta, fecha),
        "seguro": _vigente(veh.seguro_carga_hasta, fecha) if veh.seguro_carga_hasta else "n/a",
        "conduccion": _vigente(cond.licencia_conduccion_vigente_hasta, fecha),
        "transito": "X" if veh.licencia_transito_vigente else "NO",
    })

    planificador = ""
    if d.planificado_por_id:
        u = d.planificado_por
        planificador = (u.get_full_name() or u.get_username()).strip()

    return {
        "conductor": cond.nombre,
        "cargo": cond.cargo,
        "fecha": _fecha_larga(fecha),
        "salida": _hora(d.hora_salida),
        "llegada_estimada": _hora(d.hora_llegada_estimada),
        "llegada_real": _hora(d.hora_llegada_real),
        "ruta_lineas": ruta_lineas,
        "destino": ruta.destino,
        "vehiculo": f"{veh.placa} – {veh.tipo}",
        "marcas": marcas,
        "otros": otros,
        "riesgos": _riesgos_de_ruta(ruta),
        "puntos_criticos": _puntos_criticos(ruta),
        "paradas": _lineas(d.puntos_parada_segura) or ["Ninguna"],
        "sin_descanso": d.tiempo_conduccion_sin_descanso or "Ninguno",
        "descansos": _lineas(d.tiempos_descanso_programados) or ["1. Ninguno", "2. Ninguno"],
        "clima": d.condiciones_climaticas_esperadas or "",
        "alcoholemia_fecha": _fecha_hora(d.alcoholemia_fecha_hora),
        "alcoholemia_resultado": _lineas(d.alcoholemia_resultado),
        "responsable": planificador,
    }



def _llenar_documento(doc, datos):
    
    t = _tabla(doc, "INFORMACIÓN GENERAL")
    for etiqueta, clave in (
        ("nombre del conductor", "conductor"),
        ("cargo", "cargo"),
        ("fecha del desplazamiento", "fecha"),
        ("hora de salida", "salida"),
        ("hora de llegada estimada", "llegada_estimada"),
        ("hora de llegada real", "llegada_real"),
    ):
        _escribir_celda(_fila(t, etiqueta).cells[1], [datos[clave]])

    
    t = _tabla(doc, "RUTA Y DESTINO")
    _escribir_celda(_fila(t, "ruta a seguir", despues=True).cells[0], datos["ruta_lineas"])
    _escribir_celda(_fila(t, "destino final", despues=True).cells[0], [datos["destino"]])

    
    t = _tabla(doc, "VEHÍCULO Y EQUIPO USADO")
    _escribir_celda(_fila(t, "vehiculo asignado").cells[1], [datos["vehiculo"]])
    _escribir_celda(_fila(t, "equipo adicional").cells[1], EQUIPO_ADICIONAL)

    
    t = _tabla(doc, "INSPECCIÓN DEL VEHÍCULO")
    filas_check = (
        ("neumatic", "neumatic"), ("luces", "luces"), ("freno", "freno"),
        ("direccion", "direccion"), ("liquido", "liquido"),
        ("soat", "soat"), ("revision", "revision"), ("seguro", "seguro"),
        ("conduccion", "conduccion"), ("transito", "transito"),
    )
    for etiqueta, clave in filas_check:
        fila = _fila(t, etiqueta, col_etiqueta=1)
        _escribir_celda(fila.cells[2], [datos["marcas"].get(clave, "")])
    if datos["otros"]:
        _agregar_texto(_fila(t, "otros", col_etiqueta=1).cells[1], " " + datos["otros"])

    
    vineta = _numid_vineta(doc)
    t = _tabla(doc, "PLAN DE SEGURIDAD Y RIESGOS")
    _escribir_vinetas(_fila(t, "riesgos asociados", despues=True).cells[0], datos["riesgos"], vineta)
    _escribir_celda(_fila(t, "puntos criticos", despues=True).cells[0], datos["puntos_criticos"])
    _escribir_celda(_fila(t, "puntos de parada", despues=True).cells[0], datos["paradas"])
    _escribir_vinetas(_fila(t, "accion preventiva", despues=True).cells[0], ACCION_PREVENTIVA, vineta)

   
    t = _tabla(doc, "TIEMPOS DE DESCANSO Y CONDUCCIÓN")
    _escribir_celda(_fila(t, "tiempo estimado").cells[1], [datos["sin_descanso"]])
    _escribir_celda(_fila(t, "programados").cells[1], datos["descansos"])

    t = _tabla(doc, "REVISIÓN DE NORMAS DE TRÁNSITO")
    _escribir_vinetas(_fila(t, "limites de velocidad").cells[1], LIMITES_VELOCIDAD, vineta)
    _escribir_celda(_fila(t, "condiciones climaticas").cells[1], [datos["clima"]] if datos["clima"] else [])

    
    t = _tabla(doc, "CONTROL DE ALCOHOLEMIA")
    _escribir_celda(_fila(t, "fecha y hora programada").cells[1],
                    [datos["alcoholemia_fecha"]] if datos["alcoholemia_fecha"] else [])
    _escribir_celda(_fila(t, "resultado").cells[1], datos["alcoholemia_resultado"])

    
    t = _tabla(doc, "NOMBRE DEL RESPONSABLE")
    celda = t.rows[1].cells[0]
    parrafos = celda._tc.findall(qn("w:p"))
    
    destino = next((p for p in parrafos if p.find(qn("w:pPr")) is not None
                    and p.find(qn("w:pPr")).find(qn("w:jc")) is not None), parrafos[0])
    _reemplazar_texto_de_parrafo(destino, datos["responsable"])


def generar_word_control_planificacion(desplazamiento, plantilla_path=None, salida_path=None, viajes=None):
    """Llena la plantilla con los datos del Desplazamiento y devuelve la ruta del .docx.
    `viajes`: viajes GPS del día del desplazamiento (para la ruta a seguir)."""
    from django.conf import settings

    base_dir = Path(settings.BASE_DIR)
    plantilla = Path(plantilla_path) if plantilla_path else (
        base_dir / "plantillas" / "plantilla_control_planificacion.docx")
    if not os.path.exists(plantilla):
        raise FileNotFoundError(f"No se encontró la plantilla Word en: {plantilla}")

    if salida_path is None:
        salida_dir = Path(settings.MEDIA_ROOT) / "reportes"
        os.makedirs(salida_dir, exist_ok=True)
        salida_path = salida_dir / f"Control_Planificacion_{desplazamiento.id}.docx"

    doc = docx.Document(str(plantilla))
    _llenar_documento(doc, armar_datos(desplazamiento, viajes=viajes))
    doc.save(str(salida_path))
    return salida_path