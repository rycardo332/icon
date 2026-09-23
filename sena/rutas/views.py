from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import render

from .importador_filpac import ErrorImportacion, importar_reporte
from .models import Viaje


@login_required(login_url="/admin/login/")
def importar_gps(request):
    resumen = None
    if request.method == "POST":
        archivo = request.FILES.get("archivo")
        if not archivo:
            messages.error(request, "Selecciona un archivo Excel de FILPAC.")
        elif not archivo.name.lower().endswith(".xlsx"):
            messages.error(request, "El archivo debe ser .xlsx (el reporte de FILPAC).")
        else:
            try:
                resumen = importar_reporte(archivo, request.user)
                messages.success(request, "Importacion terminada.")
            except ErrorImportacion as e:
                messages.error(request, str(e))

    viajes = Viaje.objects.select_related("vehiculo").order_by("-fecha_hora_inicio")[:50]
    return render(request, "gps/importar.html", {"resumen": resumen, "viajes": viajes})