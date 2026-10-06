import os
import io
import re
import logging
import unicodedata
from functools import wraps
from pathlib import Path
from datetime import date, datetime, time, timedelta
from urllib.parse import urlencode

from django import forms
from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import User
from django.contrib.auth.views import LoginView
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.db.models import Count, Max, Min, ProtectedError, Q
from django.http import FileResponse, Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.dateparse import parse_date
from django.utils.html import escape
from django.views.decorators.clickjacking import xframe_options_sameorigin
from django.views.decorators.http import require_POST

from .resumen_dia import resumen_del_dia, municipio
from .narrativa_ruta_osrm import generar_texto_ruta, ErrorOSRM
from .usuarios import enviar_enlace_contrasena
from . import resumen_dia as rd
from .panel import panel
from . import indicador_velocidad as iv
from . import generador_excel, generador_word, mapa_viajes
from . import excel_fondo
from .importador_filpac import ErrorImportacion, importar_reporte
from .forms import PlanificacionForm, RutaForm, VehiculoForm, ConductorForm
from .validadores import ArchivoInvalido, MAX_ARCHIVOS, validar_xlsx
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

logger = logging.getLogger(__name__)

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
    Debajo del mapa se guarda el trazado; la ruta se busca o se crea sola."""
    vehiculo_sel = request.GET.get("vehiculo", "")
    fecha_sel = request.GET.get("fecha", "")
    riesgos_sel = request.GET.get("riesgos", "1") != "0"
    query = urlencode(
        {k: request.GET[k] for k in ("vehiculo", "fecha", "riesgos") if request.GET.get(k)}
    )
    context = {
        "vehiculos": Vehiculo.objects.filter(activo=True).order_by("placa"),
        "conductores": Conductor.objects.filter(activo=True).order_by("nombre"),
        "vehiculo_sel": vehiculo_sel,
        "fecha_sel": fecha_sel,
        "riesgos_sel": riesgos_sel,
        "mostrar_mapa": bool(vehiculo_sel and fecha_sel),
        "query": query,
    }
    return render(request, "gps/mapa.html", context)


def _siguiente_codigo_ruta():
    """R-001, R-002... el siguiente número libre."""
    mayor = 0
    for codigo in Ruta.objects.values_list("codigo", flat=True):
        m = re.fullmatch(r"R-(\d+)", codigo or "")
        if m:
            mayor = max(mayor, int(m.group(1)))
    return f"R-{mayor + 1:03d}"


def _ruta_para_viajes(viajes, vehiculo, fecha):
    """Busca la ruta del catálogo para la salida del día; si no existe, la crea.

    Usa el mismo resumen que la pantalla de Rutas (destino limpio: 'Tasco', 'Iza').
    Devuelve (ruta, fue_creada). Si no hay salida de Duitama devuelve (None, False)."""
    salidas = [
        s for s in resumen_del_dia(fecha)
        if s["vehiculo"] and s["vehiculo"].pk == vehiculo.pk
    ]
    if not salidas:
        return None, False

    # Si hubo varias salidas ese día, la que llegó más lejos
    s = max(salidas, key=lambda x: x["km_hasta_destino"])
    origen, destino = s["origen"], s["destino"]
    if not destino:
        return None, False

    # ¿Ya existe una ruta con ese destino? (compara el municipio limpio)
    candidatas = [
        r for r in Ruta.objects.all()
        if (r.destino or "").strip().lower() == destino.lower()
        or municipio(r.destino).lower() == destino.lower()
    ]
    if candidatas:
        candidatas.sort(key=lambda r: (not r.activa, r.codigo))  # activas primero
        ruta = candidatas[0]
        if not ruta.activa:
            ruta.activa = True
            ruta.save(update_fields=["activa"])
        return ruta, False

    ruta = Ruta.objects.create(
        codigo=_siguiente_codigo_ruta(),
        nombre=f"{origen} - {destino}"[:200],
        origen=origen,
        destino=destino,
        distancia_km=s["km_gps"],
        tiempo_estimado=s["hora_fin"] - s["hora_salida"],
        activa=True,
    )
    return ruta, True


@login_required
def guardar_trazado(request):
    """Guarda el trazado del día (vehículo + fecha) como un Desplazamiento.

    La ruta se busca sola por el destino del GPS y, si no existe, se crea.
    (Si el formulario manda una ruta concreta, se usa esa.)
    Si ya existe un Desplazamiento de esa ruta, vehículo y fecha, se actualiza."""
    if request.method != "POST":
        return redirect("listar_rutas")

    vehiculo_id = request.POST.get("vehiculo", "")
    ruta_id = request.POST.get("ruta", "")
    conductor_id = request.POST.get("conductor", "")
    try:
        fecha = parse_date(request.POST.get("fecha", ""))
    except ValueError:
        fecha = None

    volver = reverse("listar_rutas") + "?" + urlencode({"fecha": request.POST.get("fecha", "")})

    vehiculo = Vehiculo.objects.filter(pk=vehiculo_id).first() if vehiculo_id.isdigit() else None
    conductor = Conductor.objects.filter(pk=conductor_id).first() if conductor_id.isdigit() else None

    if not (vehiculo and fecha):
        messages.error(request, "Elige un vehículo y una fecha antes de guardar el trazado.")
        return redirect("listar_rutas")
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

    ruta = Ruta.objects.filter(pk=ruta_id).first() if ruta_id.isdigit() else None
    creada = False
    if not ruta:
        ruta, creada = _ruta_para_viajes(viajes, vehiculo, fecha)
    if not ruta:
        messages.error(
            request,
            "El GPS no trae el destino de este viaje, así que no se pudo crear la ruta sola.",
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
        nueva = " (ruta nueva creada)" if creada else ""
        messages.success(
            request, f"Trazado del {fecha:%d/%m/%Y} guardado en la ruta {ruta.codigo}{nueva}."
        )

    return redirect(f"{reverse('ver_tarjeta_ruta', args=[ruta.id])}?desplazamiento={d.pk}")


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


def _generar_excel_tarjeta(ruta_id, desplazamiento_id, con_riesgos):
    ruta = Ruta.objects.get(pk=ruta_id)
    desplazamiento = None
    if desplazamiento_id:
        desplazamiento = Desplazamiento.objects.select_related(
            "vehiculo", "viaje_gps"
        ).get(pk=desplazamiento_id)
    if desplazamiento:
        viajes = viajes_del_desplazamiento(desplazamiento)
    else:
        viajes = list(Viaje.objects.filter(ruta=ruta).order_by("-fecha_hora_inicio")[:1])
    return generador_excel.generar_excel_tarjeta_ruta(
        ruta, viajes=viajes, con_riesgos=con_riesgos, desplazamiento=desplazamiento
    )


@login_required
def descargar_tarjeta_ruta(request, ruta_id):
    """Genera y descarga la tarjeta de ruta en Excel, con los datos y el mapa del
    desplazamiento seleccionado (?desplazamiento=ID; por defecto, el más reciente).

    Si la petición viene del JavaScript de la página (X-Requested-With), la generación
    corre en segundo plano y se responde enseguida: así se puede cambiar de módulo
    sin perder el Excel. Si ya hay uno vigente, queda listo de inmediato.
    ?regenerar=1 fuerza volver a construirlo desde cero."""
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

    sufijo = f"_{desplazamiento.fecha_desplazamiento:%Y-%m-%d}" if desplazamiento else ""
    nombre_descarga = f"Tarjeta_Ruta_{ruta.id}{sufijo}.xlsx"

    # ---------- NUEVO: en segundo plano ----------
    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        desp_id = desplazamiento.pk if desplazamiento else None
        clave = f"tarjeta-{ruta.id}-{desp_id or 0}-{int(con_riesgos)}"
        titulo = f"Excel de la tarjeta de ruta {ruta.codigo}"
        if desplazamiento:
            titulo += f" ({desplazamiento.fecha_desplazamiento:%d/%m/%Y})"
        if ya_generado:
            excel_fondo.registrar_listo(request.user.pk, clave, titulo, nombre_descarga, salida_path)
        else:
            excel_fondo.iniciar(
                request.user.pk, clave, titulo, nombre_descarga,
                lambda: _generar_excel_tarjeta(ruta.id, desp_id, con_riesgos),
            )
        return JsonResponse({"ok": True})

    # ---------- Camino normal (sin JavaScript), igual que antes ----------
    if not ya_generado:
        try:
            salida_path = _generar_excel_tarjeta(
                ruta.id, desplazamiento.pk if desplazamiento else None, con_riesgos
            )
        except Exception:
            logger.exception("Error generando el Excel de la tarjeta de ruta")
            messages.error(
                request,
                "No se pudo generar el Excel. Intenta de nuevo o avisa al administrador.",
            )
            return redirect("ver_tarjeta_ruta", ruta_id=ruta.id)

    if not os.path.exists(salida_path):
        raise Http404("No se pudo generar el archivo Excel.")

    response = FileResponse(
        open(salida_path, "rb"),
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
    response["Content-Disposition"] = f'attachment; filename="{nombre_descarga}"'
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
    except Exception:
        logger.exception("Error generando el Word de planificación")
        messages.error(
            request,
            "No se pudo generar el Word. Intenta de nuevo o avisa al administrador.",
        )
        return redirect("ver_tarjeta_ruta", ruta_id=desplazamiento.ruta_id)

    if not os.path.exists(salida_path):
        raise Http404("No se pudo generar el archivo Word.")

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


# ----------------------------------------------------------------------
# RUTAS
# ----------------------------------------------------------------------
@login_required
def listar_rutas(request):
    """Rutas: salidas del día según el GPS + catálogo de rutas."""
    try:
        fecha = parse_date(request.GET.get("fecha", "")) or timezone.localdate()
    except ValueError:
        fecha = timezone.localdate()

    rutas = list(
        Ruta.objects
        .annotate(
            n_desp=Count("desplazamientos", distinct=True),
            n_docs=Count("documentos_generados", distinct=True),
            n_viajes=Count("viajes", distinct=True),
        )
        .order_by("codigo")
    )
    rutas_activas = [r for r in rutas if r.activa]

    salidas = rd.resumen_del_dia(fecha)
    for s in salidas:
        s["ruta_sugerida"] = _ruta_sugerida(s["destino"], rutas_activas)

    return render(request, "gps/rutas_lista.html", {
        "fecha": fecha,
        "salidas": salidas,
        "rutas": rutas,
        "rutas_activas": rutas_activas,
        "conductores": Conductor.objects.filter(activo=True).order_by("nombre"),
    })


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
    tiene_historial = (
        n_desplazamientos
        or ruta.documentos_generados.exists()
        or ruta.viajes.exists()
    )

    if request.method == "POST":
        if request.POST.get("accion") == "desactivar":
            ruta.activa = False
            ruta.save(update_fields=["activa"])
            messages.success(request, f"La ruta {ruta.codigo} quedó desactivada.")
            return redirect("listar_rutas")

        if tiene_historial:
            messages.warning(
                request,
                "Esta ruta tiene historial (trazados, documentos o viajes). "
                "Desactívala en lugar de eliminarla.",
            )
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


# ----------------------------------------------------------------------
# INSPECCIONES
# ----------------------------------------------------------------------
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


# ----------------------------------------------------------------------
# VEHÍCULOS Y CONDUCTORES
# ----------------------------------------------------------------------
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


# ----------------------------------------------------------------------
# INDICADOR DE VELOCIDAD
# ----------------------------------------------------------------------
def _anio_pedido(request):
    hoy = timezone.localdate()
    try:
        return int(request.GET.get("anio", hoy.year))
    except (TypeError, ValueError):
        return hoy.year


@login_required
def indicador_velocidad(request):
    """Indicador PESV de excesos de velocidad: tabla mensual, resumen anual y dos gráficas."""
    hoy = timezone.localdate()
    anio = _anio_pedido(request)
    resumen = iv.resumen_para_pagina(
        iv.completar_con_plantilla(iv.calcular_indicadores(anio), anio)
    )

    placas = sorted(resumen)
    placa = request.GET.get("placa", "")
    if placa not in resumen:
        placa = placas[0] if placas else ""

    meta_pct = round(iv.META * 100, 2)
    info = resumen.get(placa)
    grafica = None
    if info:
        for f in info["meses"]:
            f["pct100"] = round(f["pct"] * 100, 2) if f["pct"] is not None else None
        pa = info["pct_anual"]
        info["pct_anual100"] = round(pa * 100, 2) if pa is not None else None
        info["cumple_anual"] = (pa <= iv.META) if pa is not None else None
        grafica = {
            "meta": meta_pct,
            "labels": [f["mes"] for f in info["meses"]],
            "pct": [f["pct100"] for f in info["meses"]],
            "total": [round(f["total"], 2) if f["total"] is not None else None for f in info["meses"]],
            "exceso": [round(f["exceso"], 2) if f["exceso"] is not None else None for f in info["meses"]],
        }

    return render(request, "gps/indicador_velocidad.html", {
        "anio": anio,
        "anios": range(iv.ANIO_INICIAL, max(hoy.year, iv.ANIO_INICIAL) + 1),
        "placas": placas,
        "placa": placa,
        "info": info,
        "grafica": grafica,
        "meta_pct": meta_pct,
        "limite": iv.limite_por_defecto(),
    })


@login_required
def descargar_indicador_velocidad(request):
    """Genera el Excel del indicador (plantilla + datos del año) y lo descarga."""
    anio = _anio_pedido(request)
    buffer = io.BytesIO()
    try:
        iv.generar_excel(iv.calcular_indicadores(anio), anio, buffer)
    except Exception:
        logger.exception("Error generando el Excel del indicador de velocidad")
        messages.error(
            request,
            "No se pudo generar el Excel del indicador. Avisa al administrador.",
        )
        return redirect("indicador_velocidad")
    buffer.seek(0)
    return FileResponse(
        buffer,
        as_attachment=True,
        filename=f"Indicador_Velocidad_{anio}.xlsx",
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


# ----------------------------------------------------------------------
# LOGIN
# ----------------------------------------------------------------------
class LoginRecordarme(LoginView):
    template_name = "login_rutas.html"
    redirect_authenticated_user = True

    def form_valid(self, form):
        response = super().form_valid(form)
        if not self.request.POST.get("remember_me"):
            self.request.session.set_expiry(0)  # la sesión se cierra al cerrar el navegador
        return response


# ----------------------------------------------------------------------
# ADMINISTRACIÓN DE USUARIOS
# ----------------------------------------------------------------------
def solo_admin(vista):
    """Solo staff. Si está logueado pero no es staff, responde 403 (no lo manda al login)."""
    @wraps(vista)
    def envoltura(request, *args, **kwargs):
        if not (request.user.is_active and request.user.is_staff):
            raise PermissionDenied
        return vista(request, *args, **kwargs)
    return envoltura


class UsuarioForm(forms.ModelForm):
    class Meta:
        model = User
        fields = ("username", "first_name", "last_name", "email", "is_staff")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["email"].required = True

    def clean_email(self):
        email = self.cleaned_data["email"].strip().lower()
        otros = User.objects.filter(email__iexact=email).exclude(pk=self.instance.pk)
        if otros.exists():
            raise forms.ValidationError("Ya existe un usuario con ese correo.")
        return email


@login_required
@solo_admin
def listar_usuarios(request):
    usuarios = User.objects.order_by("-is_active", "username")
    return render(request, "gps/usuarios_lista.html", {"usuarios": usuarios})


@login_required
@solo_admin
def crear_usuario(request):
    form = UsuarioForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        usuario = form.save(commit=False)
        usuario.set_unusable_password()  # la define la persona desde el correo
        usuario.save()
        try:
            enviar_enlace_contrasena(request, usuario)
            messages.success(request, f"Usuario creado. Se envió el enlace a {usuario.email}.")
        except Exception:
            logger.exception("No se pudo enviar el enlace de contraseña")
            messages.warning(
                request,
                "Usuario creado, pero no se pudo enviar el correo. "
                "Usa «Reenviar enlace» en la lista.",
            )
        return redirect("listar_usuarios")
    return render(request, "gps/usuario_form.html", {"form": form})


@login_required
@solo_admin
def editar_usuario(request, pk):
    usuario = get_object_or_404(User, pk=pk)
    form = UsuarioForm(request.POST or None, instance=usuario)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, "Usuario actualizado.")
        return redirect("listar_usuarios")
    return render(request, "gps/usuario_form.html", {"form": form, "usuario": usuario})


@login_required
@solo_admin
@require_POST
def reenviar_enlace_usuario(request, pk):
    usuario = get_object_or_404(User, pk=pk)
    if not usuario.email:
        messages.warning(request, f"{usuario.username} no tiene correo.")
    else:
        try:
            enviar_enlace_contrasena(request, usuario)
            messages.success(request, f"Enlace enviado a {usuario.email}.")
        except Exception:
            logger.exception("No se pudo reenviar el enlace de contraseña")
            messages.error(request, f"No se pudo enviar el correo a {usuario.email}.")
    return redirect("listar_usuarios")


@login_required
@solo_admin
@require_POST
def alternar_usuario(request, pk):
    usuario = get_object_or_404(User, pk=pk)
    if usuario == request.user:
        messages.error(request, "No puedes desactivar tu propia cuenta.")
    else:
        usuario.is_active = not usuario.is_active
        usuario.save(update_fields=["is_active"])
        estado = "activado" if usuario.is_active else "desactivado"
        messages.success(request, f"Usuario {usuario.username} {estado}.")
    return redirect("listar_usuarios")


# ----------------------------------------------------------------------
# UTILIDADES
# ----------------------------------------------------------------------
def _norm(texto):
    texto = unicodedata.normalize("NFD", texto or "")
    return "".join(c for c in texto if unicodedata.category(c) != "Mn").lower().strip()


def _ruta_sugerida(destino, rutas):
    d = _norm(destino)
    if not d:
        return None
    for r in rutas:
        destino_ruta = _norm(r.destino)
        if destino_ruta and (d in destino_ruta or destino_ruta in d):
            return r
    return None


from . import importacion_fondo as fondo


# ----------------------------------------------------------------------
# IMPORTACIÓN DE REPORTES GPS (FILPAC)
# ----------------------------------------------------------------------
@login_required
def importar_gps(request):
    """Sube los reportes Excel de FILPAC. La importación corre en segundo plano."""
    if request.method == "POST":
        ajax = request.headers.get("X-Requested-With") == "XMLHttpRequest"
        archivos = request.FILES.getlist("archivo_excel")

        error, codigo = None, 400
        if not archivos:
            error = "No se recibió ningún archivo. Selecciona el .xlsx e intenta de nuevo."
        elif len(archivos) > MAX_ARCHIVOS:
            error = f"Máximo {MAX_ARCHIVOS} archivos por importación."
        else:
            try:
                datos = []
                for a in archivos:
                    nombre = validar_xlsx(a)
                    datos.append((nombre, a.read()))
            except ArchivoInvalido as e:
                error = str(e)
            else:
                if not fondo.iniciar(request.user, datos):
                    error, codigo = "Ya hay una importación en curso. Espera a que termine.", 409

        if ajax:
            if error:
                return JsonResponse({"ok": False, "mensaje": error}, status=codigo)
            return JsonResponse({"ok": True})
        if error:
            messages.error(request, error)
        return redirect("importar_gps")

    # GET: si hay una importación terminada que no se ha mostrado, se muestra ahora
    resumen = None
    entrega = fondo.entregar_terminado(request.user.pk)
    if entrega:
        resumen = entrega["resumen"]
        for e in entrega["errores"]:
            messages.error(request, e)
        if resumen:
            messages.success(request, "Importación terminada exitosamente.")

    ultimos_viajes = Viaje.objects.select_related("vehiculo").order_by("-fecha_hora_inicio")[:10]
    return render(request, "gps/importar.html", {
        "resumen": resumen,
        "ultimos_viajes": ultimos_viajes,
    })


@login_required
def importar_estado(request):
    """JSON con el avance de la importación del usuario (la barra de progreso lo consulta)."""
    return JsonResponse(fondo.estado(request.user.pk))


def _pagina_mensaje(texto):
    return HttpResponse(
        "<body style='font-family:sans-serif;padding:2rem;background:#0b1424;color:#dbe4f0'>"
        f"{escape(texto)}</body>"
    )


# ----------------------------------------------------------------------
# EXCEL EN SEGUNDO PLANO
# ----------------------------------------------------------------------
@login_required
def excel_fondo_estado(request):
    """JSON con los Excel del usuario que se están generando o están listos."""
    trabajos = excel_fondo.listar(request.user.pk)
    for t in trabajos:
        t["url"] = reverse("excel_fondo_archivo", args=[t["clave"]])
        t["cerrar"] = reverse("excel_fondo_cerrar", args=[t["clave"]])
    return JsonResponse({"trabajos": trabajos})


@login_required
def excel_fondo_archivo(request, clave):
    """Descarga el Excel ya generado."""
    t = excel_fondo.obtener(request.user.pk, clave)
    if not t or t["estado"] != "listo" or not os.path.exists(t["ruta_archivo"] or ""):
        raise Http404("Ese Excel ya no está disponible. Vuelve a generarlo.")
    excel_fondo.marcar_visto(request.user.pk, clave)
    return FileResponse(
        open(t["ruta_archivo"], "rb"),
        as_attachment=True,
        filename=t["nombre_descarga"],
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


@login_required
def excel_fondo_cerrar(request, clave):
    """Oculta el aviso (error o Excel ya descargado)."""
    excel_fondo.marcar_visto(request.user.pk, clave)
    return JsonResponse({"ok": True})