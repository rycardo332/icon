"""Tiempos de descanso y de conducción calculados con el reporte GPS de FILPAC.

El GPS parte el recorrido del día en varios viajes cada vez que el vehículo se detiene.
Cada pausa entre un viaje y el siguiente se toma como un descanso: se sabe dónde fue
(el destino del viaje anterior), a qué hora llegó y a qué hora volvió a salir.

    from .descansos import descansos_desde_gps
    datos = descansos_desde_gps(viajes)   # None si no hay viajes
    datos["sin_descanso"]   -> "47 min"   (el tramo más largo manejando seguido)
    datos["descansos"]      -> ["1. 9 min en KRA 27 CON CLL 11, SOGAMOSO (8:10 am – 8:18 am)"]
"""
import re
from datetime import timedelta

from django.utils import timezone

# Pausas más cortas que esto no cuentan como descanso (se toman como parte del mismo tramo).
MIN_DESCANSO_MIN = 3


def _local(dt):
    if dt is not None and timezone.is_aware(dt):
        return timezone.localtime(dt)
    return dt


def _hora(dt):
    dt = _local(dt)
    return f"{dt.hour % 12 or 12}:{dt.minute:02d} {'am' if dt.hour < 12 else 'pm'}"


def _duracion(td):
    minutos = max(0, int(round(td.total_seconds() / 60)))
    horas, resto = divmod(minutos, 60)
    if horas == 0:
        return f"{resto} min"
    if resto == 0:
        return f"{horas} h"
    return f"{horas} h {resto:02d} min"


def _lugar(texto):
    """'30 m al Noreste de KRA 27 CON CLL 11, SOGAMOSO, BOYACA' -> 'KRA 27 CON CLL 11, SOGAMOSO'."""
    t = (texto or "").strip()
    if not t:
        return ""
    t = re.sub(r"^\s*\d+([.,]\d+)?\s*(m|km)\s+al\s+\S+\s+de\s+", "", t, flags=re.I)
    partes = [p.strip() for p in t.split(",") if p.strip()]
    if len(partes) >= 3:
        partes = partes[:-1]  # quita el departamento
    return ", ".join(partes)


def descansos_desde_gps(viajes):
    """{'sin_descanso': str, 'descansos': [str, ...]} o None si no hay viajes con hora."""
    viajes = sorted(
        (v for v in (viajes or []) if v.fecha_hora_inicio and v.fecha_hora_fin),
        key=lambda v: v.fecha_hora_inicio,
    )
    if not viajes:
        return None

    minimo = timedelta(minutes=MIN_DESCANSO_MIN)
    primero = viajes[0]
    ini, fin, ultimo = primero.fecha_hora_inicio, primero.fecha_hora_fin, primero
    tramos, pausas = [], []

    for v in viajes[1:]:
        pausa = v.fecha_hora_inicio - fin
        if pausa >= minimo:
            tramos.append(fin - ini)
            pausas.append((_lugar(ultimo.direccion_destino), fin, v.fecha_hora_inicio, pausa))
            ini = v.fecha_hora_inicio
        if v.fecha_hora_fin >= fin:
            fin, ultimo = v.fecha_hora_fin, v
    tramos.append(fin - ini)

    lineas = []
    for i, (lugar, desde, hasta, pausa) in enumerate(pausas, 1):
        donde = f" en {lugar}" if lugar else ""
        lineas.append(f"{i}. {_duracion(pausa)}{donde} ({_hora(desde)} – {_hora(hasta)})")

    return {
        "sin_descanso": _duracion(max(tramos)),
        "descansos": lineas or ["Ninguno"],
    }