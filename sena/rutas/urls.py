from django.urls import path

from . import views

urlpatterns = [
    path("gps/importar/", views.importar_gps, name="importar_gps"),
]