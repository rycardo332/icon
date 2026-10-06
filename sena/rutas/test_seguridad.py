"""Pruebas de seguridad básicas del acceso al sistema."""
from unittest import skipUnless

from django.conf import settings
from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.shortcuts import resolve_url
from django.test import TestCase
from django.urls import reverse

from . import excel_fondo

# Páginas que NUNCA deben abrirse sin iniciar sesión
URLS_PRIVADAS = [
    "panel", "importar_gps", "importar_estado", "mapa_gps", "listar_rutas",
    "listar_vehiculos", "listar_conductores", "listar_inspecciones",
    "listar_desplazamientos", "indicador_velocidad", "excel_fondo_estado",
    "listar_usuarios", "crear_usuario",
]


class AccesoTests(TestCase):
    def setUp(self):
        self.normal = User.objects.create_user("normal", "n@x.com", "Clave-Segura-123")
        self.admin = User.objects.create_user("admin1", "a@x.com", "Clave-Segura-123", is_staff=True)

    def test_sin_sesion_todo_redirige_al_login(self):
        for nombre in URLS_PRIVADAS:
            r = self.client.get(reverse(nombre))
            self.assertEqual(r.status_code, 302, f"{nombre} se abrió sin iniciar sesión")
            self.assertIn(resolve_url(settings.LOGIN_URL), r["Location"], nombre)

    def test_usuario_normal_no_entra_a_administracion_de_usuarios(self):
        self.client.force_login(self.normal)
        for nombre in ("listar_usuarios", "crear_usuario"):
            r = self.client.get(reverse(nombre))
            self.assertEqual(r.status_code, 403, f"{nombre} se abrió a un usuario normal")

    def test_admin_si_entra_a_usuarios(self):
        self.client.force_login(self.admin)
        self.assertEqual(self.client.get(reverse("listar_usuarios")).status_code, 200)

    def test_acciones_de_usuarios_solo_por_post(self):
        self.client.force_login(self.admin)
        for nombre in ("reenviar_enlace_usuario", "alternar_usuario"):
            r = self.client.get(reverse(nombre, args=[self.normal.pk]))
            self.assertEqual(r.status_code, 405, f"{nombre} acepta GET")

    def _intentar_login(self, usuario, clave):
        return self.client.post(reverse("login"), {"username": usuario, "password": clave})

    def test_contrasena_incorrecta_no_inicia_sesion(self):
        self._intentar_login("normal", "otra-clave")
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_contrasena_correcta_si_inicia_sesion(self):
        self._intentar_login("normal", "Clave-Segura-123")
        self.assertIn("_auth_user_id", self.client.session)

    @skipUnless("axes" in settings.INSTALLED_APPS, "django-axes no está instalado")
    def test_bloqueo_tras_muchos_intentos_fallidos(self):
        for _ in range(getattr(settings, "AXES_FAILURE_LIMIT", 5)):
            self._intentar_login("normal", "mala-clave")
        # Ya bloqueado: ni siquiera la contraseña correcta debe entrar
        self._intentar_login("normal", "Clave-Segura-123")
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_un_usuario_no_descarga_el_excel_de_otro(self):
        # El admin tiene un Excel "listo"; el usuario normal intenta bajarlo con la misma clave
        excel_fondo.registrar_listo(self.admin.pk, "tarjeta-1-1-1", "Excel R-001", "x.xlsx", __file__)
        self.client.force_login(self.normal)
        r = self.client.get(reverse("excel_fondo_archivo", args=["tarjeta-1-1-1"]))
        self.assertEqual(r.status_code, 404)

    def test_las_cookies_de_sesion_no_son_legibles_por_javascript(self):
        self.assertTrue(settings.SESSION_COOKIE_HTTPONLY)

    def test_importar_rechaza_archivos_que_no_son_xlsx(self):
        self.client.force_login(self.normal)
        falso = SimpleUploadedFile("reporte.xlsx", b"esto no es un excel")
        r = self.client.post(
            reverse("importar_gps"),
            {"archivo_excel": falso},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertEqual(r.status_code, 400)

    def test_mapa_escapa_caracteres_que_rompen_el_javascript(self):
        from .mapa_viajes import _h
        resultado = _h("<script>`${alert(1)}`\\")
        for caracter in ("<", ">", "`", "$", "\\"):
            self.assertNotIn(caracter, resultado)

    def test_importador_quita_el_igual_inicial_de_las_direcciones(self):
        from .importador_filpac import _direccion
        self.assertFalse(_direccion('=HYPERLINK("http://x","clic")').startswith("="))
        self.assertEqual(_direccion("Calle 5 # 10-20"), "Calle 5 # 10-20")