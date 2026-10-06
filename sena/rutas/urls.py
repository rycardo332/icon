from django.urls import path
from . import views
from . import copia_seguridad

urlpatterns = [
    # Panel principal (raíz)
    path('', views.panel, name='panel'),

    # Importador GPS
    path('gps/importar/', views.importar_gps, name='importar_gps'),

    # Mapa GPS (mapa_gps se borra al final, cuando todo funcione)
    path('gps/mapa/', views.mapa_gps, name='mapa_gps'),
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
    path('gps/inspecciones/<int:desplazamiento_id>/hacer/', views.hacer_inspeccion, name='hacer_inspeccion'),

    # Indicador de velocidad
    path('gps/indicador-velocidad/', views.indicador_velocidad, name='indicador_velocidad'),
    path('gps/indicador-velocidad/excel/', views.descargar_indicador_velocidad, name='descargar_indicador_velocidad'),

    # Usuarios (solo administradores)
    path('usuarios/', views.listar_usuarios, name='listar_usuarios'),
    path('usuarios/nuevo/', views.crear_usuario, name='crear_usuario'),
    path('usuarios/<int:pk>/editar/', views.editar_usuario, name='editar_usuario'),
    path('usuarios/<int:pk>/enlace/', views.reenviar_enlace_usuario, name='reenviar_enlace_usuario'),
    path('usuarios/<int:pk>/alternar/', views.alternar_usuario, name='alternar_usuario'),
    path('gps/importar/estado/', views.importar_estado, name='importar_estado'),
    path('excel-fondo/estado/', views.excel_fondo_estado, name='excel_fondo_estado'),
    path('excel-fondo/<str:clave>/archivo/', views.excel_fondo_archivo, name='excel_fondo_archivo'),
    path('excel-fondo/<str:clave>/cerrar/', views.excel_fondo_cerrar, name='excel_fondo_cerrar'),

    # Copia de seguridad (solo administradores)
    path('copia-seguridad/', copia_seguridad.descargar_backup, name='descargar_backup'),
    path('copia-seguridad/restaurar/', copia_seguridad.restaurar_backup, name='restaurar_backup'),
]