from django import forms
from .models import Conductor, Desplazamiento, Ruta, Vehiculo


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


class PlanificacionForm(forms.ModelForm):
    """Datos del Control de Planificación (SIG-FT-77) que no salen del GPS."""

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