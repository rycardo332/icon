"""gps/alertas.py

Calcula los documentos por vencer (SOAT, tecnomecánica, seguro de carga y
licencia de conducción) y los entrega a todas las plantillas para la campana.
"""
from datetime import timedelta

from django.urls import NoReverseMatch, reverse
from django.utils import timezone

from .models import Conductor, Vehiculo

DIAS_AVISO = 30  # cuántos días antes del vencimiento se avisa


def _url(nombre, pk):
    """Enlace de edición; si el nombre de la URL no existe, la alerta sale sin enlace."""
    try:
        return reverse(nombre, args=[pk])
    except NoReverseMatch:
        return None


def obtener_alertas(dias_aviso=DIAS_AVISO):
    """Lista de alertas ordenada por fecha (primero lo ya vencido).

    Cada alerta es un dict con: quien, doc, fecha, dias, vencido, url.
    `dias` siempre es positivo: días que faltan o días que lleva vencido.
    Solo se revisan vehículos y conductores activos.
    """
    hoy = timezone.localdate()
    limite = hoy + timedelta(days=dias_aviso)
    alertas = []

    def agregar(quien, doc, fecha, url):
        if fecha is None or fecha > limite:
            return
        dias = (fecha - hoy).days
        alertas.append({
            "quien": quien,
            "doc": doc,
            "fecha": fecha,
            "dias": abs(dias),
            "vencido": dias < 0,
            "url": url,
        })

    for v in Vehiculo.objects.filter(activo=True):
        url = _url("editar_vehiculo", v.pk)
        agregar(v.placa, "SOAT", v.soat_vigente_hasta, url)
        agregar(v.placa, "Revisión técnico mecánica", v.revision_tecnomecanica_hasta, url)
        agregar(v.placa, "Seguro de carga", v.seguro_carga_hasta, url)

    for c in Conductor.objects.filter(activo=True):
        agregar(
            c.nombre,
            "Licencia de conducción",
            c.licencia_conduccion_vigente_hasta,
            _url("editar_conductor", c.pk),
        )

    alertas.sort(key=lambda a: a["fecha"])
    return alertas


def alertas_documentos(request):
    """Context processor: deja las alertas disponibles en todas las plantillas."""
    user = getattr(request, "user", None)
    if not user or not user.is_authenticated:
        return {}
    alertas = obtener_alertas()
    return {
        "alertas_docs": alertas,
        "alertas_n": len(alertas),
        "alertas_vencidas_n": sum(1 for a in alertas if a["vencido"]),
    }