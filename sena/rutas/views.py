import os
from pathlib import Path
from datetime import date, datetime, time, timedelta
from urllib.parse import urlencode
from .narrativa_ruta_osrm import generar_texto_ruta, ErrorOSRM
from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import Count, Max, Min, Q
from django.http import FileResponse, Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.dateparse import parse_date
from django.utils.html import escape
from django.views.decorators.clickjacking import xframe_options_sameorigin
from .panel import panel
from . import generador_excel, generador_word, mapa_viajes
from .importador_filpac import ErrorImportacion, importar_reporte
from django.db.models import ProtectedError
from .forms import PlanificacionForm, RutaForm, VehiculoForm, ConductorForm
from django.db import transaction
from .models import (
    Conductor,
    Desplazamiento,
    RegistroGPS,
    Ruta,
    Vehiculo,
    Viaje,
    CatalogoItemInspeccion, InspeccionVehiculo, ItemInspeccion,
    # OJO: con V mayúscula (es el nombre de la clase)
)

# Solo es la etiqueta de la capa "Maniobras menores" en el mapa; pon aquí el mismo
# valor (en km) que usa tu importador para separar viajes de maniobras.
UMBRAL_MANIOBRA_KM = 0.5


def rango_del_dia(fecha):
    """(inicio, fin) del día como rango de fechas. Se usa en lugar de `__date`, que en
    MySQL devuelve vacío si no están cargadas las tablas de zona horaria."""
    inicio = datetime.combine(fecha, time.min)
    if settings.USE_TZ:
        inicio = timezone.make_aware(inicio)
    return inicio, inicio + timedelta(days=1)


def viajes_del_dia(vehiculo, fecha):
    """Todos los viajes GPS del vehículo ese día, del primero al último."""
    ini, fin = rango_del_dia(fecha)
    return list(
        Viaje.objects.filter(
            vehiculo=vehiculo, fecha_hora_inicio__gte=ini, fecha_hora_inicio__lt=fin
        ).order_by("fecha_hora_inicio")
    )


def viajes_del_desplazamiento(desplazamiento):
    """El recorrido completo de la jornada: todos los viajes del vehículo ese día."""
    return viajes_del_dia(desplazamiento.vehiculo, desplazamiento.fecha_desplazamiento)


def _hora_local(dt):
    """Hora (sin microsegundos) de un datetime, en hora local si es aware."""
    if timezone.is_aware(dt):
        dt = timezone.localtime(dt)
    return dt.time().replace(microsecond=0)


@login_required
def importar_gps(request):
    """Vista para subir y procesar los reportes Excel de FILPAC."""
    resumen = None
    if request.method == "POST" and request.FILES.get("archivo_excel"):
        archivo = request.FILES["archivo_excel"]
        try:
            resumen = importar_reporte(archivo, request.user)
            messages.success(request, "Importación terminada exitosamente.")
        except ErrorImportacion as e:
            messages.error(request, str(e))
        except Exception as e:
            messages.error(request, f"Error inesperado al procesar el archivo: {str(e)}")
    elif request.method == "POST":
        messages.error(request, "No se recibió ningún archivo. Selecciona el .xlsx e intenta de nuevo.")

    # La importación crea objetos Viaje (no Desplazamiento)
    ultimos_viajes = (
        Viaje.objects.select_related("vehiculo")
    .order_by("-fecha_hora_inicio")[:10]
    )

    context = {
        "resumen": resumen,
        "ultimos_viajes": ultimos_viajes,
    }
    return render(request, "gps/importar.html", context)


@login_required
def tarjeta_ruta_inicio(request):
    """Atajo: /gps/tarjeta_ruta/ abre la tarjeta de la primera ruta activa."""
    ruta = (
        Ruta.objects.filter(activa=True).order_by("id").first()
        or Ruta.objects.order_by("id").first()
    )
    if not ruta:
        messages.error(request, "Aún no hay rutas creadas. Crea una desde el admin.")
        return redirect("importar_gps")
    return redirect("ver_tarjeta_ruta", ruta_id=ruta.id)


# ----------------------------------------------------------------------
# MAPA DE VIAJES (usa mapa_viajes.py)
# ----------------------------------------------------------------------
@login_required
def mapa_gps(request):
    """Pantalla con el formulario (vehículo, fecha, riesgos) y el mapa dentro de un iframe.
    Debajo del mapa se puede guardar el trazado en una Ruta con la fecha elegida."""
    vehiculo_sel = request.GET.get("vehiculo", "")
    fecha_sel = request.GET.get("fecha", "")
    ruta_sel = request.GET.get("ruta", "")
    riesgos_sel = request.GET.get("riesgos", "1") != "0"
    query = urlencode(
        {k: request.GET[k] for k in ("vehiculo", "fecha", "riesgos") if request.GET.get(k)}
    )
    context = {
        "vehiculos": Vehiculo.objects.filter(activo=True).order_by("placa"),
        "rutas": Ruta.objects.order_by("codigo"),
        "conductores": Conductor.objects.filter(activo=True).order_by("nombre"),
        "vehiculo_sel": vehiculo_sel,
        "fecha_sel": fecha_sel,
        "ruta_sel": ruta_sel,
        "riesgos_sel": riesgos_sel,
        "mostrar_mapa": bool(vehiculo_sel and fecha_sel),
        "query": query,
    }
    return render(request, "gps/mapa.html", context)


@login_required
def guardar_trazado(request):
    """Guarda el trazado del mapa (vehículo + fecha) como un Desplazamiento de la Ruta elegida.

    Así una misma Ruta puede tener varios trazados en fechas distintas: cada uno es un
    Desplazamiento con su fecha, y la Tarjeta de Ruta / el Excel usan la fecha del trazado
    que se elija. Si ya existe un Desplazamiento de esa ruta, vehículo y fecha, se actualiza
    en lugar de duplicarlo."""
    if request.method != "POST":
        return redirect("mapa_gps")

    vehiculo_id = request.POST.get("vehiculo", "")
    ruta_id = request.POST.get("ruta", "")
    conductor_id = request.POST.get("conductor", "")
    try:
        fecha = parse_date(request.POST.get("fecha", ""))
    except ValueError:
        fecha = None

    volver = reverse("mapa_gps") + "?" + urlencode({
        "vehiculo": vehiculo_id,
        "fecha": request.POST.get("fecha", ""),
        "ruta": ruta_id,
        "riesgos": request.POST.get("riesgos", "1"),
    })

    vehiculo = Vehiculo.objects.filter(pk=vehiculo_id).first() if vehiculo_id.isdigit() else None
    ruta = Ruta.objects.filter(pk=ruta_id).first() if ruta_id.isdigit() else None
    conductor = Conductor.objects.filter(pk=conductor_id).first() if conductor_id.isdigit() else None

    if not (vehiculo and fecha):
        messages.error(request, "Elige un vehículo y una fecha antes de guardar el trazado.")
        return redirect("mapa_gps")
    if not ruta:
        messages.error(request, "Elige la ruta donde se va a guardar el trazado.")
        return redirect(volver)
    if not conductor:
        messages.error(request, "Elige el conductor del desplazamiento.")
        return redirect(volver)

    viajes = viajes_del_dia(vehiculo, fecha)
    if not viajes:
        messages.error(
            request,
            f"No hay viajes GPS de {vehiculo.placa} el {fecha:%d/%m/%Y}; no se guardó el trazado.",
        )
        return redirect(volver)

    hora_salida = _hora_local(viajes[0].fecha_hora_inicio)
    hora_llegada = _hora_local(viajes[-1].fecha_hora_fin)

    d = Desplazamiento.objects.filter(
        ruta=ruta, vehiculo=vehiculo, fecha_desplazamiento=fecha
    ).first()
    if d:
        d.conductor = conductor
        d.viaje_gps = viajes[0]
        d.estado_emparejamiento = Desplazamiento.EstadoEmparejamiento.MANUAL
        d.save()
        messages.success(
            request, f"Se actualizó el trazado del {fecha:%d/%m/%Y} en la ruta {ruta.codigo}."
        )
    else:
        d = Desplazamiento.objects.create(
            ruta=ruta,
            conductor=conductor,
            vehiculo=vehiculo,
            fecha_desplazamiento=fecha,
            hora_salida=hora_salida,
            hora_llegada_estimada=hora_llegada,
            hora_llegada_real=hora_llegada,
            viaje_gps=viajes[0],
            estado_emparejamiento=Desplazamiento.EstadoEmparejamiento.MANUAL,
            planificado_por=request.user,
        )
        messages.success(
            request, f"Trazado del {fecha:%d/%m/%Y} guardado en la ruta {ruta.codigo}."
        )

    return redirect(f"{reverse('ver_tarjeta_ruta', args=[ruta.id])}?desplazamiento={d.pk}")


def _pagina_mensaje(texto):
    return HttpResponse(
        "<body style='font:15px system-ui,sans-serif;color:#555;display:flex;align-items:center;"
        f"justify-content:center;height:90vh;text-align:center;padding:16px'>{escape(texto)}</body>"
    )


@login_required
@xframe_options_sameorigin  # sin esto Django bloquea el mapa dentro del iframe
def mapa_gps_render(request):
    """HTML del mapa (Folium). Se usa dentro de un iframe.

    Parámetros GET: `desplazamiento` (id)  ó  `vehiculo` (id) + `fecha` (AAAA-MM-DD);
    `riesgos=1` para incluir hospitales, peajes, radares, etc. (más lento)."""
    con_riesgos = request.GET.get("riesgos", "1") != "0"
    desplazamiento_id = request.GET.get("desplazamiento", "")

    if desplazamiento_id.isdigit():
        d = get_object_or_404(
            Desplazamiento.objects.select_related("vehiculo", "viaje_gps"), pk=desplazamiento_id
        )
        vehiculo, fecha = d.vehiculo, d.fecha_desplazamiento
        viajes = viajes_del_desplazamiento(d)
    else:
        vehiculo_id = request.GET.get("vehiculo", "")
        try:
            fecha = parse_date(request.GET.get("fecha", ""))
        except ValueError:
            fecha = None
        vehiculo = Vehiculo.objects.filter(pk=vehiculo_id).first() if vehiculo_id.isdigit() else None
        if not vehiculo or not fecha:
            return _pagina_mensaje("Elige un vehículo y una fecha para ver el mapa.")
        viajes = viajes_del_dia(vehiculo, fecha)

    filtro_gps = Q(objeto_gps=vehiculo.placa)
    if vehiculo.id_objeto_gps:
        filtro_gps |= Q(objeto_gps=vehiculo.id_objeto_gps)
    ini, fin = rango_del_dia(fecha)
    maniobras = list(
        RegistroGPS.objects.filter(
            filtro_gps, viaje__isnull=True, inicio__gte=ini, inicio__lt=fin
        )
    )

    infos = []
    for n, v in enumerate(viajes, 1):
        info = mapa_viajes.preparar_viaje(v, n)
        if info:
            infos.append(info)

    _, curvas = mapa_viajes.detectar_curvas_de_viajes(infos)

    puntos_riesgo, avisos = [], []
    if con_riesgos:
        coords = [p for i in infos if i["ruta_real"] for p in i["geometria"]]
        if coords:
            try:
                puntos_riesgo = mapa_viajes.buscar_puntos_riesgo(coords)
            except mapa_viajes.ErrorOverpass:
                avisos.append("No se pudieron cargar los puntos de riesgo (Overpass no respondió).")

    titulo = f"{vehiculo.placa} · {fecha:%d/%m/%Y}"
    html_mapa = mapa_viajes.generar_mapa_html(
        infos, maniobras, puntos_riesgo, curvas, UMBRAL_MANIOBRA_KM, titulo, avisos
    )
    if html_mapa is None:
        resumen = Viaje.objects.filter(vehiculo=vehiculo).aggregate(
            n=Count("id"), primero=Min("fecha_hora_inicio"), ultimo=Max("fecha_hora_inicio")
        )
        registros_dia = RegistroGPS.objects.filter(
            filtro_gps, inicio__gte=ini, inicio__lt=fin
        ).count()
        detalle = (
            f"Viajes ese día: {len(viajes)} (con coordenadas válidas: {len(infos)}). "
            f"Registros GPS crudos ese día: {registros_dia}. "
            f"Viajes del vehículo en total: {resumen['n']}"
        )
        if resumen["n"]:
            def _loc(dt):
                return timezone.localtime(dt) if timezone.is_aware(dt) else dt

            p, u = _loc(resumen["primero"]), _loc(resumen["ultimo"])
            detalle += f" (del {p:%d/%m/%Y} al {u:%d/%m/%Y})"
        return _pagina_mensaje(
            f"No hay viajes GPS para {vehiculo.placa} el {fecha:%d/%m/%Y}. {detalle}."
        )
    return HttpResponse(html_mapa)


# ----------------------------------------------------------------------
# TARJETA DE RUTA
# ----------------------------------------------------------------------
@login_required
def ver_tarjeta_ruta(request, ruta_id):
    """
    Vista interactiva de la Tarjeta de Ruta:
    muestra el mapa, las condiciones de la vía y los desplazamientos (trazados por fecha),
    y guarda las condiciones de la vía en la Ruta (se llenan una sola vez
    y se reutilizan en cada descarga, no cambian día a día).

    Con ?desplazamiento=ID se elige la fecha/trazado; por defecto, el más reciente.
    """
    ruta = get_object_or_404(Ruta, id=ruta_id)

    desplazamientos = (
        Desplazamiento.objects.filter(ruta=ruta)
        .select_related("vehiculo", "conductor", "viaje_gps")
        .order_by("fecha_desplazamiento", "hora_salida")
    )

    desplazamiento_id = request.POST.get("desplazamiento") or request.GET.get("desplazamiento")
    if desplazamiento_id and str(desplazamiento_id).isdigit():
        desplazamiento = get_object_or_404(desplazamientos, pk=desplazamiento_id)
    else:
        desplazamiento = desplazamientos.last()

    # ---------------- Guardar ----------------
    if request.method == "POST":
        ruta.visibilidad = request.POST.getlist("visibilidad")
        ruta.trafico = request.POST.get("trafico", "")
        ruta.tipo_superficie = request.POST.getlist("tipo_superficie")
        ruta.condiciones_generales = request.POST.getlist("condiciones_generales")

        ruta.tiene_restriccion_horario = request.POST.get("tiene_restriccion") == "si"
        ruta.restriccion_desde = request.POST.get("restriccion_desde") or None
        ruta.restriccion_hasta = request.POST.get("restriccion_hasta") or None
        ruta.restriccion_dias = request.POST.get("restriccion_dias", "")

        ruta.cuerpos_de_agua = request.POST.get("cuerpos_de_agua", "")
        ruta.puntos_apoyo_emergencia = request.POST.get("puntos_apoyo_emergencia", "")
        ruta.save()

        # NUEVO: borra los Excel viejos de esta ruta para que se regeneren con los datos nuevos
        carpeta = Path(settings.MEDIA_ROOT) / "reportes"
        for patron in (f"Tarjeta_Ruta_{ruta.id}.xlsx", f"Tarjeta_Ruta_{ruta.id}_d*.xlsx"):
            for f in carpeta.glob(patron):
                f.unlink(missing_ok=True)

        messages.success(request, "La tarjeta de ruta se guardó correctamente.")
        return redirect(f"{request.path}?desplazamiento={desplazamiento.pk if desplazamiento else ''}")

    # ---------------- Mostrar ----------------
    context = {
        "ruta": ruta,
        "desplazamientos": desplazamientos,
        "desplazamiento": desplazamiento,
        "today": date.today(),
    }
    return render(request, "gps/tarjeta_ruta.html", context)


@login_required
def descargar_tarjeta_ruta(request, ruta_id):
    """Genera y descarga la tarjeta de ruta en Excel, con los datos y el mapa del
    desplazamiento (trazado por fecha) seleccionado (?desplazamiento=ID; por defecto, el más
    reciente). El resumen mensual del Excel cuenta todos los trazados de la ruta.

    Si ya existe un Excel generado para este mismo Desplazamiento y sigue vigente
    (no más viejo que la última actualización del caché de Overpass), se reusa tal cual
    en vez de regenerarlo. ?regenerar=1 fuerza volver a construirlo desde cero."""
    ruta = get_object_or_404(Ruta, id=ruta_id)

    desplazamientos = (
        Desplazamiento.objects.filter(ruta=ruta)
        .select_related("vehiculo", "viaje_gps")
        .order_by("fecha_desplazamiento", "hora_salida")
    )
    desplazamiento_id = request.GET.get("desplazamiento", "")
    if desplazamiento_id.isdigit():
        desplazamiento = get_object_or_404(desplazamientos, pk=desplazamiento_id)
    else:
        desplazamiento = desplazamientos.last()

    if desplazamiento:
        viajes = viajes_del_desplazamiento(desplazamiento)
    else:
        viajes = list(Viaje.objects.filter(ruta=ruta).order_by("-fecha_hora_inicio")[:1])

    con_riesgos = request.GET.get("riesgos", "1") != "0"
    forzar = request.GET.get("regenerar", "") == "1"

    sufijo_disp = f"_d{desplazamiento.pk}" if desplazamiento is not None else ""
    salida_path = Path(settings.MEDIA_ROOT) / "reportes" / f"Tarjeta_Ruta_{ruta.id}{sufijo_disp}.xlsx"

    ya_generado = False
    if not forzar and salida_path.exists() and desplazamiento is not None \
            and desplazamiento.cache_entorno_actualizado is not None:
        import datetime as _dt
        mtime_archivo = datetime.utcfromtimestamp(salida_path.stat().st_mtime)
        actualizado = desplazamiento.cache_entorno_actualizado
        if timezone.is_aware(actualizado):
            actualizado_utc = actualizado.astimezone(_dt.timezone.utc).replace(tzinfo=None)
        else:
            actualizado_utc = actualizado
        ya_generado = mtime_archivo >= actualizado_utc

    if not ya_generado:
        try:
            salida_path = generador_excel.generar_excel_tarjeta_ruta(
                ruta, viajes=viajes, con_riesgos=con_riesgos, desplazamiento=desplazamiento
            )
        except Exception as e:
            messages.error(request, f"No se pudo generar el Excel: {e}")
            return redirect("ver_tarjeta_ruta", ruta_id=ruta.id)

    if not os.path.exists(salida_path):
        raise Http404(f"No se pudo generar el archivo Excel en: {salida_path}")

    sufijo = f"_{desplazamiento.fecha_desplazamiento:%Y-%m-%d}" if desplazamiento else ""
    response = FileResponse(
        open(salida_path, "rb"),
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
    response["Content-Disposition"] = f'attachment; filename="Tarjeta_Ruta_{ruta.id}{sufijo}.xlsx"'
    return response


@login_required
def descargar_word_control_planificacion(request, desplazamiento_id):
    """Genera y descarga el Control de Planificación de Desplazamientos Laborales
    (SIG-FT-77, Word) del desplazamiento indicado."""
    desplazamiento = get_object_or_404(
        Desplazamiento.objects.select_related("ruta", "vehiculo", "conductor", "planificado_por"),
        pk=desplazamiento_id,
    )

    try:
        salida_path = generador_word.generar_word_control_planificacion(
            desplazamiento, viajes=viajes_del_desplazamiento(desplazamiento)
        )
    except Exception as e:
        messages.error(request, f"No se pudo generar el Word: {e}")
        return redirect("ver_tarjeta_ruta", ruta_id=desplazamiento.ruta_id)

    if not os.path.exists(salida_path):
        raise Http404(f"No se pudo generar el archivo Word en: {salida_path}")

    nombre = f"Control_Planificacion_{desplazamiento.fecha_desplazamiento:%Y-%m-%d}_{desplazamiento.vehiculo.placa}.docx"
    response = FileResponse(
        open(salida_path, "rb"),
        content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    )
    response["Content-Disposition"] = f'attachment; filename="{nombre}"'
    return response


@login_required
def planificar_desplazamiento(request, desplazamiento_id):
    """Una sola pantalla para completar el Control de Planificación (SIG-FT-77):
    datos del viaje (clima, paradas, descansos, alcoholemia) + checklist de inspección.
    Los documentos (SOAT, revisión, licencias) no se digitan: se calculan por fechas."""
    d = get_object_or_404(
        Desplazamiento.objects.select_related("ruta", "vehiculo", "conductor"),
        pk=desplazamiento_id,
    )
    items = list(
        CatalogoItemInspeccion.objects
        .exclude(categoria=CatalogoItemInspeccion.Categoria.DOCUMENTACION)
        .order_by("categoria", "nombre")
    )
    insp = InspeccionVehiculo.objects.filter(desplazamiento=d).first()
    guardados = {it.item_id: it for it in insp.items.all()} if insp else {}
    estados_validos = set(ItemInspeccion.Estado.values)
    resultados_validos = set(InspeccionVehiculo.Resultado.values)

    if request.method == "POST":
        form = PlanificacionForm(request.POST, instance=d)
        filas = [
            {
                "item": it,
                "estado": request.POST.get(f"estado_{it.id}", ""),
                "obs": request.POST.get(f"obs_{it.id}", "").strip(),
            }
            for it in items
        ]
        insp_datos = {
            "realizada_por": request.POST.get("insp_realizada_por", "").strip(),
            "resultado": request.POST.get("insp_resultado", ""),
            "observaciones": request.POST.get("insp_observaciones", "").strip(),
        }

        errores = [
            f"Escribe la observación de «{f['item'].nombre}» (marcaste que no cumple)."
            for f in filas
            if f["estado"] == ItemInspeccion.Estado.NO_CUMPLE and not f["obs"]
        ]

        if form.is_valid() and not errores:
            form.save()

            marcados = [f for f in filas if f["estado"] in estados_validos]
            if marcados or insp or insp_datos["observaciones"]:
                insp, _ = InspeccionVehiculo.objects.get_or_create(desplazamiento=d)
                insp.realizada_por = insp_datos["realizada_por"]
                insp.resultado_general = (
                    insp_datos["resultado"]
                    if insp_datos["resultado"] in resultados_validos
                    else InspeccionVehiculo.Resultado.APTO
                )
                insp.observaciones = insp_datos["observaciones"]
                insp.save()
                for f in filas:
                    if f["estado"] in estados_validos:
                        ItemInspeccion.objects.update_or_create(
                            inspeccion=insp,
                            item=f["item"],
                            defaults={"estado": f["estado"], "observacion": f["obs"]},
                        )
                    else:
                        ItemInspeccion.objects.filter(inspeccion=insp, item=f["item"]).delete()

                hay_fallas = any(f["estado"] == ItemInspeccion.Estado.NO_CUMPLE for f in filas)
                if hay_fallas and insp.resultado_general == InspeccionVehiculo.Resultado.APTO:
                    messages.warning(
                        request, "Hay ítems que no cumplen, pero el resultado general quedó como «Apto»."
                    )

            messages.success(request, "La planificación se guardó correctamente.")
            return redirect("planificar_desplazamiento", desplazamiento_id=d.pk)

        for e in errores:
            messages.error(request, e)
    else:
        form = PlanificacionForm(instance=d)
        filas = [
            {
                "item": it,
                "estado": guardados[it.id].estado if it.id in guardados else "",
                "obs": guardados[it.id].observacion if it.id in guardados else "",
            }
            for it in items
        ]
        insp_datos = {
            "realizada_por": (
                insp.realizada_por if insp
                else (request.user.get_full_name() or request.user.get_username())
            ),
            "resultado": insp.resultado_general if insp else InspeccionVehiculo.Resultado.APTO,
            "observaciones": insp.observaciones if insp else "",
        }

    v, c, fecha = d.vehiculo, d.conductor, d.fecha_desplazamiento

    def _doc(nombre, hasta):
        if hasta is None:
            return {"nombre": nombre, "hasta": None, "estado": "na"}
        return {"nombre": nombre, "hasta": hasta, "estado": "ok" if hasta >= fecha else "vencido"}

    documentos = [
        _doc("SOAT", v.soat_vigente_hasta),
        _doc("Revisión técnico mecánica", v.revision_tecnomecanica_hasta),
        _doc("Seguro de carga", v.seguro_carga_hasta),
        _doc("Licencia de conducción", c.licencia_conduccion_vigente_hasta),
        {"nombre": "Licencia de tránsito", "hasta": None,
         "estado": "ok" if v.licencia_transito_vigente else "vencido"},
    ]

    return render(request, "gps/planificar_desplazamiento.html", {
        "desplazamiento": d,
        "form": form,
        "filas": filas,
        "insp_datos": insp_datos,
        "resultados": InspeccionVehiculo.Resultado.choices,
        "documentos": documentos,
    })
    
@login_required
def listar_desplazamientos(request):
    """Lista de todos los desplazamientos, para el botón 'Ver todos' del panel."""
    desplazamientos = (
        Desplazamiento.objects.select_related("ruta", "vehiculo", "conductor")
        .order_by("-fecha_desplazamiento", "-hora_salida")
    )
    return render(request, "gps/desplazamientos.html", {"desplazamientos": desplazamientos})


# Agrega estas 4 funciones en views.py, junto a las demás vistas de Ruta.
# Necesitan estos imports adicionales, si no los tienes ya arriba del archivo:
#   from django.db.models import ProtectedError
#   from .forms import RutaForm   (agrégalo al import que ya tienes de .forms)


@login_required
def listar_rutas(request):
    """Lista de todas las rutas, con datos básicos."""
    rutas = Ruta.objects.order_by("codigo")
    return render(request, "gps/rutas_lista.html", {"rutas": rutas})


@login_required
def crear_ruta(request):
    if request.method == "POST":
        form = RutaForm(request.POST, request.FILES)
        if form.is_valid():
            form.save()
            messages.success(request, "La ruta se creó correctamente.")
            return redirect("listar_rutas")
    else:
        form = RutaForm()
    return render(request, "gps/ruta_form.html", {"form": form, "titulo": "Nueva ruta"})


@login_required
def editar_ruta(request, ruta_id):
    ruta = get_object_or_404(Ruta, pk=ruta_id)
    if request.method == "POST":
        form = RutaForm(request.POST, request.FILES, instance=ruta)
        if form.is_valid():
            form.save()
            messages.success(request, "La ruta se actualizó correctamente.")
            return redirect("listar_rutas")
    else:
        form = RutaForm(instance=ruta)
    return render(request, "gps/ruta_form.html", {
        "form": form, "titulo": f"Editar ruta {ruta.codigo}", "ruta": ruta,
    })


@login_required
def eliminar_ruta(request, ruta_id):
    ruta = get_object_or_404(Ruta, pk=ruta_id)
    n_desplazamientos = Desplazamiento.objects.filter(ruta=ruta).count()

    if request.method == "POST":
        if request.POST.get("accion") == "desactivar":
            ruta.activa = False
            ruta.save(update_fields=["activa"])
            messages.success(request, f"La ruta {ruta.codigo} quedó desactivada.")
            return redirect("listar_rutas")

        if n_desplazamientos:
            messages.warning(request, "Esta ruta tiene desplazamientos y no se puede eliminar.")
            return redirect("listar_rutas")

        try:
            ruta.delete()
            messages.success(request, "Ruta eliminada.")
        except ProtectedError:
            messages.warning(request, "Esta ruta tiene registros asociados y no se puede eliminar.")
        return redirect("listar_rutas")

    return render(request, "gps/ruta_confirmar_eliminar.html", {
        "ruta": ruta,
        "n_desplazamientos": n_desplazamientos,
    })
    
@login_required
def alternar_ruta(request, ruta_id):
    if request.method != "POST":
        return redirect("listar_rutas")
    ruta = get_object_or_404(Ruta, pk=ruta_id)
    ruta.activa = not ruta.activa
    ruta.save(update_fields=["activa"])
    estado = "activada" if ruta.activa else "desactivada"
    messages.success(request, f"La ruta {ruta.codigo} quedó {estado}.")
    return redirect("listar_rutas")

@login_required
def listar_inspecciones(request):
    solo_pendientes = request.GET.get("ver") == "pendientes"
    desplazamientos = (Desplazamiento.objects
                       .select_related("ruta", "vehiculo", "conductor", "inspeccion")
                       .order_by("-fecha_desplazamiento", "-hora_salida"))
    if solo_pendientes:
        desplazamientos = desplazamientos.filter(inspeccion__isnull=True)
    return render(request, "gps/inspecciones_lista.html", {
        "desplazamientos": desplazamientos[:200],
        "solo_pendientes": solo_pendientes,
    })


@login_required
def hacer_inspeccion(request, desplazamiento_id):
    d = get_object_or_404(
        Desplazamiento.objects.select_related("ruta", "vehiculo", "conductor"),
        pk=desplazamiento_id)
    insp = InspeccionVehiculo.objects.filter(desplazamiento=d).first()
    catalogo = CatalogoItemInspeccion.objects.order_by("categoria", "nombre")
    previos = {it.item_id: it for it in insp.items.all()} if insp else {}

    filas = []
    for c in catalogo:
        p = previos.get(c.id)
        filas.append({
            "item": c,
            "estado": p.estado if p else "cumple",
            "observacion": p.observacion if p else "",
            "error": "",
        })

    nombre_usuario = (request.user.get_full_name() or request.user.get_username()).strip()
    datos = {
        "realizada_por": insp.realizada_por if insp else nombre_usuario,
        "resultado_general": insp.resultado_general if insp else "apto",
        "observaciones": insp.observaciones if insp else "",
    }
    error_resultado = ""

    if request.method == "POST":
        estados_validos = {"cumple", "no_cumple", "no_aplica"}
        hay_error = False
        for f in filas:
            cid = f["item"].id
            f["estado"] = request.POST.get(f"estado_{cid}", "")
            f["observacion"] = request.POST.get(f"obs_{cid}", "").strip()
            if f["estado"] not in estados_validos:
                f["error"] = "Elige una opción."
                hay_error = True
            elif f["estado"] == "no_cumple" and not f["observacion"]:
                f["error"] = "Explica qué falla."
                hay_error = True

        datos = {
            "realizada_por": request.POST.get("realizada_por", "").strip(),
            "resultado_general": request.POST.get("resultado_general", ""),
            "observaciones": request.POST.get("observaciones", "").strip(),
        }
        if datos["resultado_general"] not in dict(InspeccionVehiculo.Resultado.choices):
            error_resultado = "Elige un resultado."
            hay_error = True
        elif datos["resultado_general"] == "apto" and any(f["estado"] == "no_cumple" for f in filas):
            error_resultado = "Hay ítems que no cumplen: elige «Apto con observaciones» o «No apto»."
            hay_error = True

        if not hay_error:
            with transaction.atomic():
                if insp is None:
                    insp = InspeccionVehiculo(desplazamiento=d)
                insp.realizada_por = datos["realizada_por"]
                insp.resultado_general = datos["resultado_general"]
                insp.observaciones = datos["observaciones"]
                insp.save()
                for f in filas:
                    ItemInspeccion.objects.update_or_create(
                        inspeccion=insp, item=f["item"],
                        defaults={"estado": f["estado"], "observacion": f["observacion"]})
            messages.success(request, f"Inspección de {d.vehiculo.placa} guardada.")
            return redirect("listar_inspecciones")

    return render(request, "gps/inspeccion_form.html", {
        "d": d, "filas": filas, "datos": datos,
        "error_resultado": error_resultado,
        "resultados": InspeccionVehiculo.Resultado.choices,
    })
    
def _estado_doc(fecha, hoy):
    if fecha is None:
        return {"fecha": None, "texto": "Sin fecha", "color": "secondary"}
    dias = (fecha - hoy).days
    if dias < 0:
        texto, color = "Vencido", "danger"
    elif dias <= 30:
        texto, color = f"Vence en {dias} d", "warning"
    else:
        texto, color = "Vigente", "success"
    return {"fecha": fecha, "texto": texto, "color": color}


@login_required
def listar_vehiculos(request):
    hoy = timezone.localdate()
    filas = []
    for v in Vehiculo.objects.order_by("-activo", "placa"):
        filas.append({
            "v": v,
            "docs": [
                _estado_doc(v.soat_vigente_hasta, hoy),
                _estado_doc(v.revision_tecnomecanica_hasta, hoy),
                _estado_doc(v.seguro_carga_hasta, hoy),
            ],
        })
    return render(request, "gps/vehiculos_lista.html", {"filas": filas})

@login_required
def crear_vehiculo(request):
    form = VehiculoForm(request.POST or None, request.FILES or None)
    if request.method == "POST" and form.is_valid():
        v = form.save()
        messages.success(request, f"Vehículo {v.placa} creado.")
        return redirect("listar_vehiculos")
    return render(request, "gps/vehiculo_form.html", {"form": form, "vehiculo": None})


@login_required
def editar_vehiculo(request, vehiculo_id):
    vehiculo = get_object_or_404(Vehiculo, pk=vehiculo_id)
    form = VehiculoForm(request.POST or None, request.FILES or None, instance=vehiculo)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, f"Vehículo {vehiculo.placa} actualizado.")
        return redirect("listar_vehiculos")
    return render(request, "gps/vehiculo_form.html", {"form": form, "vehiculo": vehiculo})

@login_required
def alternar_vehiculo(request, vehiculo_id):
    if request.method != "POST":
        return redirect("listar_vehiculos")
    v = get_object_or_404(Vehiculo, pk=vehiculo_id)
    v.activo = not v.activo
    v.save(update_fields=["activo"])
    estado = "activado" if v.activo else "desactivado"
    messages.success(request, f"El vehículo {v.placa} quedó {estado}.")
    return redirect("listar_vehiculos")

@login_required
def listar_conductores(request):
    hoy = timezone.localdate()
    filas = []
    for c in Conductor.objects.order_by("-activo", "nombre"):
        filas.append({
            "c": c,
            "licencia": _estado_doc(c.licencia_conduccion_vigente_hasta, hoy),
        })
    return render(request, "gps/conductores_lista.html", {"filas": filas})


@login_required
def crear_conductor(request):
    form = ConductorForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        c = form.save()
        messages.success(request, f"Conductor {c.nombre} creado.")
        return redirect("listar_conductores")
    return render(request, "gps/conductor_form.html", {"form": form, "conductor": None})


@login_required
def editar_conductor(request, conductor_id):
    conductor = get_object_or_404(Conductor, pk=conductor_id)
    form = ConductorForm(request.POST or None, instance=conductor)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, f"Conductor {conductor.nombre} actualizado.")
        return redirect("listar_conductores")
    return render(request, "gps/conductor_form.html", {"form": form, "conductor": conductor})


@login_required
def alternar_conductor(request, conductor_id):
    if request.method != "POST":
        return redirect("listar_conductores")
    c = get_object_or_404(Conductor, pk=conductor_id)
    c.activo = not c.activo
    c.save(update_fields=["activo"])
    estado = "activado" if c.activo else "desactivado"
    messages.success(request, f"El conductor {c.nombre} quedó {estado}.")
    return redirect("listar_conductores")