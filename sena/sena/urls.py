from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.contrib.auth import views as auth_views
from django.urls import include, path

from rutas.views import tarjeta_ruta_inicio

urlpatterns = [
    # Admin
    path('admin/', admin.site.urls),

    # Login (diseño nuevo) y logout
    path('login/', auth_views.LoginView.as_view(
        template_name='login_rutas.html',
        redirect_authenticated_user=True,
    ), name='login'),
    path('logout/', auth_views.LogoutView.as_view(), name='logout'),

    # Recuperación de contraseña
    path('password-reset/',
         auth_views.PasswordResetView.as_view(
             template_name='registration/password_reset_form.html',
             html_email_template_name='registration/password_reset_email.html',
             email_template_name='registration/password_reset_email.txt',
         ),
         name='password_reset'),

    path('password-reset/done/',
         auth_views.PasswordResetDoneView.as_view(
             template_name='registration/password_reset_done.html'),
         name='password_reset_done'),

    path('password-reset-confirm/<uidb64>/<token>/',
         auth_views.PasswordResetConfirmView.as_view(
             template_name='registration/password_reset_confirm.html',
             post_reset_login=False,
         ),
         name='password_reset_confirm'),

    path('password-reset-complete/',
         auth_views.PasswordResetCompleteView.as_view(
             template_name='registration/password_reset_complete.html'),
         name='password_reset_complete'),

    # Tarjeta de ruta
    path('gps/tarjeta_ruta/', tarjeta_ruta_inicio, name='tarjeta_ruta_inicio'),

    # App rutas (panel, gps, vehículos, conductores, etc.) en la raíz
    path('', include('rutas.urls')),
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)