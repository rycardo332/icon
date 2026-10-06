from datetime import datetime, timedelta

from django import forms
from django.utils import timezone

from .descansos import descansos_desde_gps
from .models import Conductor, Desplazamiento, Ruta, Vehiculo, Viaje


CLIMA = [
    ("", "— Selecciona —"),
    ("Despejado", "Despejado"),
    ("Nublado", "Nublado"),
    ("Lluvia", "Lluvia"),
    ("Neblina", "Neblina"),
    ("Lluvia y neblina", "Lluvia y neblina"),
    ("Variable", "Variable"),
]

ALCOHOLEMIA = [
    ("", "— Selecciona —"),
    ("0.0 g/L - Negativo", "0.0 g/L - Negativo"),
    ("Positivo", "Positivo"),
    ("No aplica", "No aplica"),
]


def _hora_local(dt):
    """datetime (con o sin zona horaria) -> hora local sin segundos."""
    if dt is None:
        return None
    if timezone.is_aware(dt):
        dt = timezone.localtime(dt)
    return dt.time().replace(second=0, microsecond=0)


def horas_desde_gps(desplazamiento):
    """Hora de salida y de llegada real según el reporte GPS de FILPAC.

    Se toman TODOS los viajes del vehículo en la fecha del desplazamiento (el GPS parte el
    recorrido en varios viajes cada vez que el vehículo se detiene):
      - salida  = inicio del primer viaje del día
      - llegada = fin del último viaje del día
    Devuelve {"salida", "llegada", "origen"} o None si no hay datos GPS ese día.
    """
    if desplazamiento is None or not desplazamiento.pk:
        return None

    viajes = list(
        Viaje.objects.filter(
            vehiculo_id=desplazamiento.vehiculo_id,
            fecha_hora_inicio__date=desplazamiento.fecha_desplazamiento,
        ).order_by("fecha_hora_inicio")
    )
    if not viajes:
        return None

    primero = viajes[0]
    ultimo = max(viajes, key=lambda v: v.fecha_hora_fin)
    return {
        "salida": _hora_local(primero.fecha_hora_inicio),
        "llegada": _hora_local(ultimo.fecha_hora_fin),
        "origen": f"del reporte de FILPAC (primer y último de {len(viajes)} viaje(s) del vehículo ese día)",
    }


class PlanificacionForm(forms.ModelForm):
    """Datos del Control de Planificación (SIG-FT-77).

    La hora de salida y la de llegada real salen del reporte GPS de FILPAC (si hay viajes
    importados de ese día); la llegada estimada se calcula con el tiempo estimado de la ruta.
    Si no hay datos GPS, esos campos se pueden escribir a mano.
    """

    class Meta:
        model = Desplazamiento
        fields = [
            "conductor",
            "hora_salida",
            "hora_llegada_estimada",
            "hora_llegada_real",
            "condiciones_climaticas_esperadas",
            "tiempo_conduccion_sin_descanso",
            "alcoholemia_fecha_hora",
            "alcoholemia_resultado",
            "puntos_parada_segura",
            "tiempos_descanso_programados",
        ]
        labels = {
            "conductor": "Conductor",
            "hora_salida": "Hora de salida",
            "hora_llegada_estimada": "Llegada estimada",
            "hora_llegada_real": "Llegada real",
            "condiciones_climaticas_esperadas": "Condiciones climáticas esperadas",
            "tiempo_conduccion_sin_descanso": "Tiempo de conducción sin descanso",
            "alcoholemia_fecha_hora": "Alcoholemia: fecha y hora programada",
            "alcoholemia_resultado": "Alcoholemia: resultado",
            "puntos_parada_segura": "Puntos de parada segura (uno por línea)",
            "tiempos_descanso_programados": "Tiempos de descanso programados (uno por línea)",
        }
        widgets = {
            "hora_salida": forms.TimeInput(attrs={"type": "time"}, format="%H:%M"),
            "hora_llegada_estimada": forms.TimeInput(attrs={"type": "time"}, format="%H:%M"),
            "hora_llegada_real": forms.TimeInput(attrs={"type": "time"}, format="%H:%M"),
            "alcoholemia_fecha_hora": forms.DateTimeInput(
                attrs={"type": "datetime-local"}, format="%Y-%m-%dT%H:%M"
            ),
            "puntos_parada_segura": forms.Textarea(
                attrs={"rows": 3, "placeholder": "Ej: Estación de servicio Terpel, km 12"}
            ),
            "tiempos_descanso_programados": forms.Textarea(
                attrs={"rows": 3, "placeholder": "Ej:\n1. 15 min en Sogamoso\n2. 10 min en Duitama"}
            ),
            "condiciones_climaticas_esperadas": forms.Select(choices=CLIMA),
            "tiempo_conduccion_sin_descanso": forms.TextInput(attrs={"placeholder": "Ej: 1 hora 30 min"}),
            "alcoholemia_resultado": forms.Select(choices=ALCOHOLEMIA),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        conductores = Conductor.objects.filter(activo=True)
        # Si el desplazamiento ya tiene un conductor asignado (aunque ahora esté
        # inactivo), se conserva para no alterar el dato histórico.
        if self.instance and self.instance.conductor_id:
            conductores = conductores | Conductor.objects.filter(pk=self.instance.conductor_id)
        self.fields["conductor"].queryset = conductores.order_by("nombre")

        self.fields["alcoholemia_fecha_hora"].input_formats = [
            "%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M",
        ]

        # Si el registro ya tiene un texto que no está en la lista (dato viejo),
        # se agrega como opción para no perderlo al guardar.
        for nombre, opciones in (
            ("condiciones_climaticas_esperadas", CLIMA),
            ("alcoholemia_resultado", ALCOHOLEMIA),
        ):
            actual = getattr(self.instance, nombre, "")
            if actual and actual not in dict(opciones):
                self.fields[nombre].widget.choices = opciones + [(actual, actual)]

        # --- Horas desde el GPS de FILPAC ---------------------------------
        # gps_ok / gps_nota los usa la plantilla para mostrar el aviso.
        self.gps_ok = False
        self.gps_nota = ""
        gps = horas_desde_gps(self.instance)
        if gps and gps["salida"]:
            self.gps_ok = True
            self.initial["hora_salida"] = gps["salida"]
            self.fields["hora_salida"].disabled = True
            self.fields["hora_salida"].help_text = "Tomada del reporte de FILPAC."

            if gps["llegada"]:
                self.initial["hora_llegada_real"] = gps["llegada"]
                self.fields["hora_llegada_real"].disabled = True
                self.fields["hora_llegada_real"].help_text = "Tomada del reporte de FILPAC."

            tiempo = self.instance.ruta.tiempo_estimado if self.instance.ruta_id else None
            if tiempo:
                estimada = (datetime.combine(self.instance.fecha_desplazamiento, gps["salida"]) + tiempo)
                self.initial["hora_llegada_estimada"] = estimada.time().replace(second=0, microsecond=0)
                self.fields["hora_llegada_estimada"].disabled = True
                self.fields["hora_llegada_estimada"].help_text = "Salida + tiempo estimado de la ruta."

            self.gps_nota = (f"La hora de salida y la de llegada real se toman {gps['origen']}. "
                             "La llegada estimada se calcula con el tiempo estimado de la ruta.")
        elif self.instance and self.instance.pk:
            self.gps_nota = ("No hay viajes GPS de este vehículo en esta fecha. Importa el reporte de "
                             "FILPAC o escribe las horas a mano.")

        # --- Descansos y tiempo de conducción desde el GPS de FILPAC ---------
        # Cada pausa entre un viaje y el siguiente se toma como un descanso (ver descansos.py).
        if self.instance and self.instance.pk:
            viajes_dia = list(Viaje.objects.filter(
                vehiculo_id=self.instance.vehiculo_id,
                fecha_hora_inicio__date=self.instance.fecha_desplazamiento,
            ))
            auto = descansos_desde_gps(viajes_dia)
            if auto:
                self.initial["tiempo_conduccion_sin_descanso"] = auto["sin_descanso"]
                self.initial["tiempos_descanso_programados"] = "\n".join(auto["descansos"])
                for nombre in ("tiempo_conduccion_sin_descanso", "tiempos_descanso_programados"):
                    self.fields[nombre].disabled = True
                    self.fields[nombre].help_text = "Calculado con el reporte de FILPAC."

        for campo in self.fields.values():
            es_select = isinstance(campo.widget, forms.Select)
            campo.widget.attrs["class"] = "form-select" if es_select else "form-control"


class RutaForm(forms.ModelForm):
    class Meta:
        model = Ruta
        fields = [
            "codigo", "nombre", "origen", "destino",
            "distancia_km", "tiempo_estimado",
        ]
        widgets = {
            "codigo": forms.TextInput(attrs={"class": "form-control"}),
            "nombre": forms.TextInput(attrs={"class": "form-control"}),
            "origen": forms.TextInput(attrs={"class": "form-control"}),
            "destino": forms.TextInput(attrs={"class": "form-control"}),
            "distancia_km": forms.NumberInput(attrs={"class": "form-control", "step": "0.01"}),
            "tiempo_estimado": forms.TextInput(attrs={
                "class": "form-control", "placeholder": "HH:MM:SS (ej. 02:30:00)"
            }),
        }
        labels = {
            "codigo": "Código",
            "nombre": "Nombre",
            "origen": "Origen",
            "destino": "Destino",
            "distancia_km": "Distancia (km)",
            "tiempo_estimado": "Tiempo estimado",
        }


class VehiculoForm(forms.ModelForm):
    class Meta:
        model = Vehiculo
        fields = [
            "placa", "tipo", "imagen",
            "soat_vigente_hasta", "revision_tecnomecanica_hasta", "seguro_carga_hasta",
            "licencia_transito_vigente",
        ]
        widgets = {
            "placa": forms.TextInput(attrs={"class": "form-control", "style": "text-transform:uppercase"}),
            "tipo": forms.TextInput(attrs={"class": "form-control", "placeholder": "ej. NISSAN FRONTIER - Camioneta"}),
            "imagen": forms.FileInput(attrs={"class": "form-control", "accept": "image/*"}),
            "soat_vigente_hasta": forms.DateInput(format="%Y-%m-%d", attrs={"class": "form-control", "type": "date"}),
            "revision_tecnomecanica_hasta": forms.DateInput(format="%Y-%m-%d", attrs={"class": "form-control", "type": "date"}),
            "seguro_carga_hasta": forms.DateInput(format="%Y-%m-%d", attrs={"class": "form-control", "type": "date"}),
            "licencia_transito_vigente": forms.CheckboxInput(attrs={"class": "form-check-input"}),
        }

    def clean_placa(self):
        return self.cleaned_data["placa"].strip().upper()


class ConductorForm(forms.ModelForm):
    class Meta:
        model = Conductor
        fields = ["nombre", "cargo", "licencia_conduccion_vigente_hasta"]
        labels = {
            "nombre": "Nombre completo",
            "cargo": "Cargo",
            "licencia_conduccion_vigente_hasta": "Licencia de conducción vigente hasta",
        }
        widgets = {
            "nombre": forms.TextInput(attrs={"class": "form-control"}),
            "cargo": forms.TextInput(attrs={"class": "form-control"}),
            "licencia_conduccion_vigente_hasta": forms.DateInput(
                attrs={"type": "date", "class": "form-control"},
                format="%Y-%m-%d",
            ),
        }

    def _validar_fecha_documento(self, campo):
        fecha = self.cleaned_data.get(campo)
        if fecha is None:
            return fecha
        hoy = timezone.localdate()
        if fecha < hoy - timedelta(days=365 * 2):
            raise forms.ValidationError("La fecha es demasiado antigua. Revisa el año.")
        if fecha > hoy + timedelta(days=365 * 3):
            raise forms.ValidationError("La fecha está demasiado lejos en el futuro. Revisa el año.")
        return fecha

    def clean_soat_vigente_hasta(self):
        return self._validar_fecha_documento("soat_vigente_hasta")

    def clean_revision_tecnomecanica_hasta(self):
        return self._validar_fecha_documento("revision_tecnomecanica_hasta")

    def clean_seguro_carga_hasta(self):
        return self._validar_fecha_documento("seguro_carga_hasta")