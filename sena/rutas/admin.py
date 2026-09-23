from django.contrib import admin
from .models import (
    Vehiculo, Conductor, Ruta, PuntoRuta, CatalogoRiesgo, RutaRiesgo,
    CatalogoItemInspeccion, InspeccionVehiculo, ItemInspeccion,
    Desplazamiento, ReporteGPSImportado, Viaje, RegistroGPS, DocumentoGenerado,
)


@admin.register(Vehiculo)
class VehiculoAdmin(admin.ModelAdmin):
    list_display = ("placa", "tipo", "soat_vigente_hasta", "revision_tecnomecanica_hasta")
    search_fields = ("placa",)


@admin.register(Conductor)
class ConductorAdmin(admin.ModelAdmin):
    list_display = ("nombre", "cargo", "licencia_conduccion_vigente_hasta")
    search_fields = ("nombre",)


@admin.register(Ruta)
class RutaAdmin(admin.ModelAdmin):
    list_display = ("codigo", "nombre", "origen", "destino", "activa")
    search_fields = ("codigo", "nombre")
    list_filter = ("activa",)


@admin.register(PuntoRuta)
class PuntoRutaAdmin(admin.ModelAdmin):
    list_display = ("ruta", "tipo", "nombre", "orden")
    list_filter = ("tipo",)


@admin.register(CatalogoRiesgo)
class CatalogoRiesgoAdmin(admin.ModelAdmin):
    list_display = ("nombre",)


@admin.register(RutaRiesgo)
class RutaRiesgoAdmin(admin.ModelAdmin):
    list_display = ("ruta", "riesgo")


@admin.register(CatalogoItemInspeccion)
class CatalogoItemInspeccionAdmin(admin.ModelAdmin):
    list_display = ("nombre", "categoria")
    list_filter = ("categoria",)


@admin.register(InspeccionVehiculo)
class InspeccionVehiculoAdmin(admin.ModelAdmin):
    list_display = ("desplazamiento", "fecha_hora", "resultado_general")
    list_filter = ("resultado_general",)


@admin.register(ItemInspeccion)
class ItemInspeccionAdmin(admin.ModelAdmin):
    list_display = ("inspeccion", "item", "estado")
    list_filter = ("estado",)


@admin.register(Desplazamiento)
class DesplazamientoAdmin(admin.ModelAdmin):
    list_display = ("ruta", "vehiculo", "conductor", "fecha_desplazamiento", "estado_emparejamiento")
    list_filter = ("estado_emparejamiento", "fecha_desplazamiento")
    search_fields = ("vehiculo__placa", "conductor__nombre")


@admin.register(ReporteGPSImportado)
class ReporteGPSImportadoAdmin(admin.ModelAdmin):
    list_display = ("fecha_importacion", "importado_por", "fecha_inicio_cubierta", "fecha_fin_cubierta")


@admin.register(Viaje)
class ViajeAdmin(admin.ModelAdmin):
    list_display = ("vehiculo", "ruta", "fecha_hora_inicio", "fecha_hora_fin")
    list_filter = ("fecha_hora_inicio",)


@admin.register(RegistroGPS)
class RegistroGPSAdmin(admin.ModelAdmin):
    list_display = ("objeto_gps", "inicio", "fin", "viaje")


@admin.register(DocumentoGenerado)
class DocumentoGeneradoAdmin(admin.ModelAdmin):
    list_display = ("tipo", "version", "fecha_generacion", "generado_por")
    list_filter = ("tipo",)