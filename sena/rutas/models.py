

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models


# ======================================================================
# 1. VEHÍCULOS Y CONDUCTORES
# ======================================================================

class Vehiculo(models.Model):
    placa = models.CharField(max_length=15, unique=True)
    tipo = models.CharField(max_length=150)  # "NISSAN FRONTIER - Camioneta"

    soat_vigente_hasta = models.DateField()
    revision_tecnomecanica_hasta = models.DateField()
    seguro_carga_hasta = models.DateField(null=True, blank=True)
    licencia_transito_vigente = models.BooleanField(default=True)

    # Vincula la placa con el identificador de objeto/dispositivo en FILPAC
    id_objeto_gps = models.CharField(max_length=50, blank=True)

    def __str__(self):
        return self.placa


class Conductor(models.Model):
    nombre = models.CharField(max_length=200)
    cargo = models.CharField(max_length=200)  # "Ingeniero Residente – líder de cuadrilla"
    licencia_conduccion_vigente_hasta = models.DateField()

    def __str__(self):
        return self.nombre


# ======================================================================
# 2. RUTA Y SU CONTENIDO (PUNTO_RUTA, CATALOGO_RIESGO, RUTA_RIESGO)
# ======================================================================

class Ruta(models.Model):
    codigo = models.CharField(max_length=50, unique=True)  # "DTM-LAS COLINAS"
    nombre = models.CharField(max_length=200)

    origen = models.CharField(max_length=200)
    destino = models.CharField(max_length=200)
    distancia_km = models.DecimalField(max_digits=6, decimal_places=2, null=True, blank=True)
    tiempo_estimado = models.DurationField(null=True, blank=True)

    descripcion_ruta = models.TextField(blank=True)
    instrucciones_recorrido = models.TextField(blank=True)  # giros paso a paso

    mapa_imagen = models.FileField(upload_to="rutas/mapas/", null=True, blank=True)
    geometria_ruta = models.JSONField(null=True, blank=True)  # polilínea/coordenadas, opcional

    activa = models.BooleanField(default=True)

    def __str__(self):
        return f"{self.codigo} - {self.nombre}"


class PuntoRuta(models.Model):
    """Entidad genérica para todo elemento de la ruta con ubicación puntual."""

    class TipoPunto(models.TextChoices):
        PUNTO_CRITICO = "punto_critico", "Punto crítico"
        INTERSECCION = "interseccion", "Intersección"
        ZONA_ESCOLAR = "zona_escolar", "Zona escolar"
        ZONA_URBANA = "zona_urbana", "Zona urbana"
        CUERPO_AGUA = "cuerpo_agua", "Cuerpo de agua"
        PUNTO_EMERGENCIA = "punto_emergencia", "Punto de emergencia"
        HOSPITAL = "hospital", "Hospital"
        ESTACION_SERVICIO = "estacion_servicio", "Estación de servicio"
        OTRO = "otro", "Otro"

    ruta = models.ForeignKey(Ruta, related_name="puntos", on_delete=models.CASCADE)
    tipo = models.CharField(max_length=30, choices=TipoPunto.choices)

    nombre = models.CharField(max_length=200)
    descripcion = models.TextField(blank=True)

    latitud = models.DecimalField(max_digits=10, decimal_places=6, null=True, blank=True)
    longitud = models.DecimalField(max_digits=10, decimal_places=6, null=True, blank=True)
    referencia_km = models.CharField(max_length=50, blank=True)  # alternativa si no hay coordenadas

    orden = models.PositiveIntegerField(null=True, blank=True)  # posición en el recorrido
    observaciones = models.TextField(blank=True)

    class Meta:
        ordering = ["ruta", "orden"]

    def __str__(self):
        return f"{self.get_tipo_display()}: {self.nombre} ({self.ruta.codigo})"


class CatalogoRiesgo(models.Model):
    nombre = models.CharField(max_length=200, unique=True)  # "Niebla", "Curvas pronunciadas"
    medida_preventiva_default = models.TextField(blank=True)

    def __str__(self):
        return self.nombre


class RutaRiesgo(models.Model):
    ruta = models.ForeignKey(Ruta, related_name="riesgos", on_delete=models.CASCADE)
    riesgo = models.ForeignKey(CatalogoRiesgo, related_name="rutas", on_delete=models.PROTECT)

    # Sobrescribe el texto por defecto del catálogo solo si esta ruta lo necesita distinto
    medida_preventiva_especifica = models.TextField(blank=True)

    class Meta:
        unique_together = ("ruta", "riesgo")

    def __str__(self):
        return f"{self.ruta.codigo} - {self.riesgo.nombre}"

    @property
    def medida_preventiva(self):
        return self.medida_preventiva_especifica or self.riesgo.medida_preventiva_default


# ======================================================================
# 3. INSPECCIÓN DEL VEHÍCULO
# ======================================================================

class CatalogoItemInspeccion(models.Model):
    class Categoria(models.TextChoices):
        EQUIPO = "equipo", "Equipo"
        MECANICO = "mecanico", "Mecánico"
        DOCUMENTACION = "documentacion", "Documentación"

    nombre = models.CharField(max_length=150, unique=True)  # "Botiquín", "Frenos", "SOAT"
    categoria = models.CharField(max_length=20, choices=Categoria.choices)

    def __str__(self):
        return self.nombre


class InspeccionVehiculo(models.Model):
    class Resultado(models.TextChoices):
        APTO = "apto", "Apto"
        APTO_CON_OBSERVACIONES = "apto_observaciones", "Apto con observaciones"
        NO_APTO = "no_apto", "No apto"

    desplazamiento = models.OneToOneField(
        "Desplazamiento", related_name="inspeccion", on_delete=models.CASCADE
    )
    fecha_hora = models.DateTimeField(auto_now_add=True)
    realizada_por = models.CharField(max_length=200, blank=True)  # quien la ejecuta físicamente
    resultado_general = models.CharField(
        max_length=20, choices=Resultado.choices, default=Resultado.APTO
    )

    def __str__(self):
        return f"Inspección de {self.desplazamiento}"


class ItemInspeccion(models.Model):
    class Estado(models.TextChoices):
        CUMPLE = "cumple", "Cumple"
        NO_CUMPLE = "no_cumple", "No cumple"
        NO_APLICA = "no_aplica", "No aplica"

    inspeccion = models.ForeignKey(
        InspeccionVehiculo, related_name="items", on_delete=models.CASCADE
    )
    item = models.ForeignKey(CatalogoItemInspeccion, on_delete=models.PROTECT)
    estado = models.CharField(max_length=15, choices=Estado.choices)
    observacion = models.TextField(blank=True)  # obligatorio en la práctica si estado != cumple

    class Meta:
        unique_together = ("inspeccion", "item")

    def __str__(self):
        return f"{self.item.nombre}: {self.get_estado_display()}"


# ======================================================================
# 4. DESPLAZAMIENTO (SIG.FT-77)
# ======================================================================

class Desplazamiento(models.Model):
    class EstadoEmparejamiento(models.TextChoices):
        SIN_GPS = "sin_gps", "Sin datos GPS aún"
        AUTO = "auto", "Emparejado automáticamente (único candidato)"
        MANUAL = "manual", "Emparejado manualmente"
        PENDIENTE = "pendiente", "Ambiguo — requiere revisión manual"

    ruta = models.ForeignKey(Ruta, related_name="desplazamientos", on_delete=models.PROTECT)
    # FIX: related_name explícito para consistencia con el resto del archivo
    # (antes generaba el default "desplazamiento_set")
    conductor = models.ForeignKey(
        Conductor, related_name="desplazamientos", on_delete=models.PROTECT
    )
    vehiculo = models.ForeignKey(
        Vehiculo, related_name="desplazamientos", on_delete=models.PROTECT
    )

    fecha_desplazamiento = models.DateField()
    hora_salida = models.TimeField()
    hora_llegada_estimada = models.TimeField()
    hora_llegada_real = models.TimeField(null=True, blank=True)

    viaje_gps = models.ForeignKey(
        "Viaje", null=True, blank=True, on_delete=models.SET_NULL, related_name="desplazamientos"
    )
    estado_emparejamiento = models.CharField(
        max_length=20,
        choices=EstadoEmparejamiento.choices,
        default=EstadoEmparejamiento.SIN_GPS,
    )

    tiempo_conduccion_sin_descanso = models.CharField(max_length=100, blank=True)
    tiempos_descanso_programados = models.TextField(blank=True)
    condiciones_climaticas_esperadas = models.CharField(max_length=300, blank=True)
    puntos_parada_segura = models.TextField(blank=True)

    alcoholemia_fecha_hora = models.DateTimeField(null=True, blank=True)
    alcoholemia_resultado = models.CharField(max_length=200, blank=True)

    planificado_por = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        related_name="desplazamientos_planificados",
        on_delete=models.PROTECT,
    )

    creado_en = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.ruta.codigo} - {self.fecha_desplazamiento} ({self.vehiculo.placa})"

    # Documentación del vehículo/conductor (SOAT, revisión, licencias) se calcula
    # al vuelo comparando fechas de vencimiento contra fecha_desplazamiento;
    # no se guarda como campo porque no debe digitarse cada vez.


# ======================================================================
# 5. GPS FILPAC: IMPORTACIÓN, REGISTROS CRUDOS Y VIAJE AGRUPADO
# ======================================================================

class ReporteGPSImportado(models.Model):
    archivo_original = models.FileField(upload_to="gps/importaciones/")
    fecha_importacion = models.DateTimeField(auto_now_add=True)
    importado_por = models.ForeignKey(
        settings.AUTH_USER_MODEL, related_name="reportes_importados", on_delete=models.PROTECT
    )

    fecha_inicio_cubierta = models.DateField(null=True, blank=True)
    fecha_fin_cubierta = models.DateField(null=True, blank=True)

    def __str__(self):
        return f"Importación {self.fecha_importacion:%Y-%m-%d %H:%M}"


class Viaje(models.Model):
    """Agrupación de RegistroGPS con sentido de negocio (un trayecto real)."""

    vehiculo = models.ForeignKey(Vehiculo, related_name="viajes", on_delete=models.PROTECT)
    ruta = models.ForeignKey(
        Ruta, related_name="viajes", null=True, blank=True, on_delete=models.SET_NULL
    )  # inferida cuando es posible, no siempre disponible

    fecha_hora_inicio = models.DateTimeField()
    fecha_hora_fin = models.DateTimeField()

    direccion_origen = models.CharField(max_length=300, blank=True)
    direccion_destino = models.CharField(max_length=300, blank=True)
    coordenadas_inicio = models.CharField(max_length=100, blank=True)
    coordenadas_fin = models.CharField(max_length=100, blank=True)

    distancia_total = models.DecimalField(max_digits=8, decimal_places=2, null=True, blank=True)
    duracion_total = models.DurationField(null=True, blank=True)

    velocidad_maxima = models.DecimalField(max_digits=6, decimal_places=2, null=True, blank=True)
    velocidad_promedio = models.DecimalField(max_digits=6, decimal_places=2, null=True, blank=True)

    tiempo_movimiento = models.DurationField(null=True, blank=True)
    tiempo_ralenti = models.DurationField(null=True, blank=True)
    tiempo_actividad = models.DurationField(null=True, blank=True)

    def __str__(self):
        return f"Viaje {self.vehiculo.placa} {self.fecha_hora_inicio:%Y-%m-%d %H:%M}"


class RegistroGPS(models.Model):
    """Fila/tramo crudo tal como viene del Excel de FILPAC."""

    reporte_importado = models.ForeignKey(
        ReporteGPSImportado, related_name="registros", on_delete=models.CASCADE
    )
    viaje = models.ForeignKey(
        Viaje, related_name="registros", null=True, blank=True, on_delete=models.SET_NULL
    )

    objeto_gps = models.CharField(max_length=50)  # identificador crudo de FILPAC, ej. "HCQ911"

    inicio = models.DateTimeField()
    fin = models.DateTimeField()
    direccion_inicio = models.CharField(max_length=300, blank=True)
    direccion_fin = models.CharField(max_length=300, blank=True)
    coordenadas_inicio = models.CharField(max_length=100, blank=True)
    coordenadas_fin = models.CharField(max_length=100, blank=True)

    duracion = models.DurationField(null=True, blank=True)
    distancia = models.DecimalField(max_digits=8, decimal_places=2, null=True, blank=True)
    velocidad_maxima = models.DecimalField(max_digits=6, decimal_places=2, null=True, blank=True)
    velocidad_promedio = models.DecimalField(max_digits=6, decimal_places=2, null=True, blank=True)

    tiempo_movimiento = models.DurationField(null=True, blank=True)
    tiempo_ralenti = models.DurationField(null=True, blank=True)
    tiempo_actividad = models.DurationField(null=True, blank=True)

    def __str__(self):
        return f"{self.objeto_gps} {self.inicio:%Y-%m-%d %H:%M}"


def buscar_candidatos_viaje(desplazamiento: Desplazamiento):
    """Devuelve los Viajes del GPS que podrían corresponder a este Desplazamiento.

    Si devuelve 1 -> estado 'auto'. Si devuelve >1 -> estado 'pendiente',
    se listan para que el usuario elija manualmente.
    """
    return Viaje.objects.filter(
        vehiculo=desplazamiento.vehiculo,
        fecha_hora_inicio__date=desplazamiento.fecha_desplazamiento,
        ruta=desplazamiento.ruta,
    )


# ======================================================================
# 6. DOCUMENTOS GENERADOS (con tipo explícito y versionado)
# ======================================================================

class DocumentoGenerado(models.Model):
    class TipoDocumento(models.TextChoices):
        TARJETA_RUTA = "tarjeta_ruta", "Tarjeta de Ruta (Excel)"
        CONTROL_PLANIFICACION = "control_planificacion", "Control de Planificación (Word)"

    tipo = models.CharField(max_length=25, choices=TipoDocumento.choices)

    # Exactamente uno de los dos debe estar lleno, según el tipo (validado en clean()).
    ruta = models.ForeignKey(
        Ruta, related_name="documentos_generados", null=True, blank=True, on_delete=models.PROTECT
    )
    desplazamiento = models.ForeignKey(
        Desplazamiento,
        related_name="documentos_generados",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
    )

    archivo_generado = models.FileField(upload_to="documentos/", null=True, blank=True)
    fecha_generacion = models.DateTimeField(auto_now_add=True)
    version = models.PositiveIntegerField(default=1)

    generado_por = models.ForeignKey(
        settings.AUTH_USER_MODEL, related_name="documentos_generados", on_delete=models.PROTECT
    )

    class Meta:
        ordering = ["-fecha_generacion"]

    def clean(self):
        if self.tipo == self.TipoDocumento.TARJETA_RUTA:
            if not self.ruta or self.desplazamiento:
                raise ValidationError(
                    "Un documento de tipo 'tarjeta_ruta' debe tener 'ruta' y no 'desplazamiento'."
                )
        elif self.tipo == self.TipoDocumento.CONTROL_PLANIFICACION:
            if not self.desplazamiento or self.ruta:
                raise ValidationError(
                    "Un documento de tipo 'control_planificacion' debe tener "
                    "'desplazamiento' y no 'ruta'."
                )

    # FIX: clean() no se llama solo en save(); sin esto la validación se salta
    # cada vez que se crea un DocumentoGenerado fuera de un ModelForm
    # (por ejemplo, desde una vista o un script de generación automática).
    def save(self, *args, **kwargs):
        self.full_clean()
        super().save(*args, **kwargs)

    def __str__(self):
        origen = self.ruta or self.desplazamiento
        return f"{self.get_tipo_display()} v{self.version} - {origen}"