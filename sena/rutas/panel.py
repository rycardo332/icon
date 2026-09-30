from datetime import timedelta

from django.contrib.auth.decorators import login_required
from django.db.models import Count, Max, Q
from django.shortcuts import render
from django.urls import NoReverseMatch, reverse
from django.utils import timezone

from .models import (
    Vehiculo, Conductor, Ruta, PuntoRuta, CatalogoRiesgo, RutaRiesgo,
    CatalogoItemInspeccion, InspeccionVehiculo, ItemInspeccion,
    Desplazamiento, ReporteGPSImportado, Viaje, RegistroGPS,
    DocumentoGenerado,
)
def _admin_url(nombre, *args):
    """URL del admin de Django; devuelve "" si no existe (así el panel nunca se cae)."""
    try:
        return reverse(f"admin:{nombre}", args=args)
    except NoReverseMatch:
        return ""


@login_required
def panel(request):
    """Mini dashboard: estado de todos los módulos, pendientes y alertas de documentos."""
    hoy = timezone.localdate()
    limite = hoy + timedelta(days=30)

    # ---------------- Alertas: documentos vencidos o que vencen en 30 días ----------------
    alertas = []

    def _alerta(tipo, quien, doc, fecha, url):
        if fecha is None or fecha > limite:
            return
        dias = (fecha - hoy).days
        alertas.append({"tipo": tipo, "quien": quien, "doc": doc, "fecha": fecha,
                        "dias": abs(dias), "vencido": dias < 0, "url": url})

    vehiculos = list(Vehiculo.objects.filter(activo=True).order_by("placa"))
    conductores = list(Conductor.objects.filter(activo=True).order_by("nombre"))
    for v in vehiculos:
        url = reverse("editar_vehiculo", args=[v.pk])
        _alerta("vehiculo", f"Vehículo {v.placa}", "SOAT", v.soat_vigente_hasta, url)
        _alerta("vehiculo", f"Vehículo {v.placa}", "Revisión técnico mecánica", v.revision_tecnomecanica_hasta, url)
        _alerta("vehiculo", f"Vehículo {v.placa}", "Seguro de carga", v.seguro_carga_hasta, url)
    for c in conductores:
        _alerta("conductor", c.nombre, "Licencia de conducción",
                c.licencia_conduccion_vigente_hasta, reverse("editar_conductor", args=[c.pk]))
    alertas.sort(key=lambda a: a["fecha"])

    # ---------------- Desplazamientos recientes y pendientes ----------------
    recientes = []
    consulta = Desplazamiento.objects.select_related("ruta", "vehiculo", "conductor", "inspeccion")
    for d in consulta.order_by("-fecha_desplazamiento", "-hora_salida")[:10]:
        insp = hasattr(d, "inspeccion")
        clima = bool(d.condiciones_climaticas_esperadas)
        alco = bool(d.alcoholemia_resultado)
        recientes.append({"d": d, "insp": insp, "clima": clima, "alco": alco,
                          "completo": insp and clima and alco})

    hace_30 = hoy - timedelta(days=30)
    n_pendientes = (
        Desplazamiento.objects.filter(fecha_desplazamiento__gte=hace_30)
        .filter(Q(inspeccion__isnull=True) | Q(condiciones_climaticas_esperadas="")
                | Q(alcoholemia_resultado=""))
        .count()
    )

    # ---------------- Rutas ----------------
    rutas = list(
        Ruta.objects.annotate(
            n_desp=Count("desplazamientos", distinct=True),
            n_puntos=Count("puntos", distinct=True),
            n_riesgos=Count("riesgos", distinct=True),
        ).order_by("codigo")[:50]
    )
    n_rutas_activas = sum(1 for r in rutas if r.activa)
    n_sin_condiciones = sum(1 for r in rutas if r.activa and not r.visibilidad)

    # ---------------- Cifras por módulo ----------------
    resumen_gps = Viaje.objects.aggregate(n=Count("id"), ultimo=Max("fecha_hora_inicio"))
    ultimo_reporte = ReporteGPSImportado.objects.order_by("-fecha_importacion").first()
    n_mes = Desplazamiento.objects.filter(
        fecha_desplazamiento__year=hoy.year, fecha_desplazamiento__month=hoy.month
    ).count()
    n_insp = InspeccionVehiculo.objects.count()
    n_fallas = InspeccionVehiculo.objects.exclude(
        resultado_general=InspeccionVehiculo.Resultado.APTO
    ).count()
    a_veh = sum(1 for a in alertas if a["tipo"] == "vehiculo")
    a_cond = sum(1 for a in alertas if a["tipo"] == "conductor")

    def _enlaces(*pares):
        return [(texto, url) for texto, url in pares if url]

    modulos = [
        {"nombre": "GPS y mapa", "icono": "fa-satellite-dish", "color": "primary",
         "cifra": resumen_gps["n"], "etiqueta": "viajes importados",
         "detalle": (f"Último reporte: {timezone.localtime(ultimo_reporte.fecha_importacion):%d/%m/%Y}"
                     if ultimo_reporte else "Aún no se ha importado ningún reporte"),
         "enlaces": _enlaces(("Importar reporte", reverse("importar_gps")),
                             ("Mapa de viajes", reverse("mapa_gps")))},
        {"nombre": "Rutas", "icono": "fa-route", "color": "success",
         "cifra": n_rutas_activas, "etiqueta": "rutas activas",
         "detalle": f"{n_sin_condiciones} sin condiciones de la vía registradas",
         "enlaces": _enlaces(("Rutas", reverse("listar_rutas")))},
        {"nombre": "Desplazamientos", "icono": "fa-truck-fast", "color": "warning",
         "cifra": n_mes, "etiqueta": "este mes",
         "detalle": f"{n_pendientes} por completar (últimos 30 días)",
         "enlaces": _enlaces(("Ver todos", reverse("listar_desplazamientos")))},
        {"nombre": "Inspección", "icono": "fa-screwdriver-wrench", "color": "info",
         "cifra": n_insp, "etiqueta": "inspecciones",
         "detalle": f"{n_fallas} con observaciones o no aptas",
                  "enlaces": _enlaces(("Inspecciones", reverse("listar_inspecciones")))},
        {"nombre": "Vehículos", "icono": "fa-car", "color": "secondary",
         "cifra": len(vehiculos), "etiqueta": "vehículos",
         "detalle": f"{a_veh} documentos vencidos o por vencer",
            "enlaces": _enlaces(("Vehículos", reverse("listar_vehiculos")))},
        {"nombre": "Conductores", "icono": "fa-id-card", "color": "dark",
         "cifra": len(conductores), "etiqueta": "conductores",
         "detalle": f"{a_cond} licencias vencidas o por vencer",
         "enlaces": _enlaces(("Conductores", reverse("listar_conductores")))},
    ]

    return render(request, "gps/panel.html", {
        "hoy": hoy,
        "modulos": modulos,
        "recientes": recientes,
        "alertas": alertas,
        "rutas": rutas,
        "n_pendientes": n_pendientes,
        "admin_inicio": _admin_url("index"),
    })
    