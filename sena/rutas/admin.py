from django import forms
from django.contrib import admin, messages
from django.contrib.auth.admin import UserAdmin
from django.contrib.auth.forms import AdminUserCreationForm
from django.contrib.auth.models import User

from .models import (
    Vehiculo, Conductor, Ruta, PuntoRuta, CatalogoRiesgo, RutaRiesgo,
    CatalogoItemInspeccion, InspeccionVehiculo, ItemInspeccion,
    Desplazamiento, ReporteGPSImportado, Viaje, RegistroGPS, DocumentoGenerado,
)
from .usuarios import enviar_enlace_contrasena


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


# ======================================================================
# USUARIOS: el admin los crea y cada persona define su contraseña por correo
# ======================================================================
@admin.action(description="Enviar enlace para establecer/restablecer contraseña")
def enviar_enlace(modeladmin, request, queryset):
    enviados = 0
    for u in queryset:
        if not u.email:
            modeladmin.message_user(request, f"{u.username} no tiene correo.", messages.WARNING)
            continue
        try:
            enviar_enlace_contrasena(request, u)
            enviados += 1
        except Exception as e:
            modeladmin.message_user(request, f"No se pudo enviar a {u.email}: {e}", messages.ERROR)
    if enviados:
        modeladmin.message_user(request, f"Enlace enviado a {enviados} usuario(s).", messages.SUCCESS)


class UsuarioAddForm(AdminUserCreationForm):
    class Meta(AdminUserCreationForm.Meta):
        model = User
        fields = ("username", "email", "first_name", "last_name")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["email"].required = True

    def clean_email(self):
        email = self.cleaned_data["email"].strip().lower()
        if User.objects.filter(email__iexact=email).exists():
            raise forms.ValidationError("Ya existe un usuario con ese correo.")
        return email


class UsuarioAdmin(UserAdmin):
    add_form = UsuarioAddForm
    add_fieldsets = (
        (None, {
            "classes": ("wide",),
            "fields": ("username", "email", "first_name", "last_name",
                       "usable_password", "password1", "password2"),
        }),
    )
    list_display = ("username", "email", "first_name", "last_name", "is_active", "last_login")
    actions = [enviar_enlace]

    def save_model(self, request, obj, form, change):
        super().save_model(request, obj, form, change)
        # Usuario nuevo sin contraseña: se le manda el enlace automáticamente
        if not change and not obj.has_usable_password() and obj.email:
            try:
                enviar_enlace_contrasena(request, obj)
                self.message_user(request, f"Se envió el enlace a {obj.email}.", messages.SUCCESS)
            except Exception as e:
                self.message_user(
                    request,
                    f"Usuario creado, pero no se pudo enviar el correo ({e}). "
                    "Usa la acción «Enviar enlace…».",
                    messages.WARNING,
                )


admin.site.unregister(User)
admin.site.register(User, UsuarioAdmin)