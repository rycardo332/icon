from django.urls import path
from . import views

urlpatterns = [
    # Panel Principal (Raíz)
    path('', views.panel, name='panel'),

    # Importador GPS
    path('gps/importar/', views.importar_gps, name='importar_gps'),

    # Mapas GPS
    path('gps/mapa/', views.mapa_gps, name='mapa_gps'), # Corregido el name
    path('gps/mapa/ver/', views.mapa_gps_render, name='mapa_gps_render'),
    path('gps/mapa/guardar/', views.guardar_trazado, name='guardar_trazado'),

    # Desplazamientos
    path('gps/desplazamientos/', views.listar_desplazamientos, name='listar_desplazamientos'),
    path('desplazamiento/<int:desplazamiento_id>/planificar/', views.planificar_desplazamiento, name='planificar_desplazamiento'),
    path('desplazamiento/<int:desplazamiento_id>/exportar-word/', views.descargar_word_control_planificacion, name='exportar_word_control_planificacion'),

    # Rutas
    path('gps/rutas/', views.listar_rutas, name='listar_rutas'),
    path('gps/rutas/nueva/', views.crear_ruta, name='crear_ruta'),
    path('gps/rutas/<int:ruta_id>/editar/', views.editar_ruta, name='editar_ruta'),
    path('gps/rutas/<int:ruta_id>/eliminar/', views.eliminar_ruta, name='eliminar_ruta'),
    path('gps/rutas/<int:ruta_id>/alternar/', views.alternar_ruta, name='alternar_ruta'),
    path('ruta/<int:ruta_id>/ver/', views.ver_tarjeta_ruta, name='ver_tarjeta_ruta'),
    path('ruta/<int:ruta_id>/exportar-excel/', views.descargar_tarjeta_ruta, name='exportar_tarjeta_ruta'),

    # Vehículos
    path('gps/vehiculos/', views.listar_vehiculos, name='listar_vehiculos'),
    path('gps/vehiculos/nuevo/', views.crear_vehiculo, name='crear_vehiculo'),
    path('gps/vehiculos/<int:vehiculo_id>/editar/', views.editar_vehiculo, name='editar_vehiculo'),
    path('gps/vehiculos/<int:vehiculo_id>/alternar/', views.alternar_vehiculo, name='alternar_vehiculo'),

    # Conductores
    path('gps/conductores/', views.listar_conductores, name='listar_conductores'),
    path('gps/conductores/nuevo/', views.crear_conductor, name='crear_conductor'),
    path('gps/conductores/<int:conductor_id>/editar/', views.editar_conductor, name='editar_conductor'),
    path('gps/conductores/<int:conductor_id>/alternar/', views.alternar_conductor, name='alternar_conductor'),

    # Inspecciones
    path('gps/inspecciones/', views.listar_inspecciones, name='listar_inspecciones'),
]