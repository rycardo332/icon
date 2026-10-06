import os
from datetime import timedelta
from pathlib import Path
from django.contrib.messages import constants as messages
from dotenv import load_dotenv
import dj_database_url

BASE_DIR = Path(__file__).resolve().parent.parent

# ========== CARGA DE VARIABLES DE ENTORNO (.env) ==========
load_dotenv(BASE_DIR / '.env')

# Si DEBUG falta en el .env, vale False (seguro por defecto).
# En tu computador pon DEBUG=True en el .env local.
DEBUG = os.environ.get('DEBUG', 'False') == 'True'
SECRET_KEY = os.environ.get('SECRET_KEY')
if not SECRET_KEY:
    if DEBUG:
        SECRET_KEY = 'django-insecure-solo-para-desarrollo-local'
    else:
        raise RuntimeError('Falta SECRET_KEY en el archivo .env')

# True cuando Django corre detrás de un proxy (Render, Nginx). Se activa en las
# variables de entorno del servidor.
BEHIND_PROXY = os.environ.get('BEHIND_PROXY', 'False') == 'True'

# Ejemplo en .env:  ALLOWED_HOSTS=localhost,127.0.0.1,mi-dominio.com
# En el servidor deja SOLO tu dominio (los enlaces de recuperación de contraseña se arman con él).
ALLOWED_HOSTS = [
    h.strip()
    for h in os.environ.get('ALLOWED_HOSTS', 'localhost,127.0.0.1').split(',')
    if h.strip()
]
# Ejemplo en .env:  CSRF_TRUSTED_ORIGINS=https://mi-dominio.com
CSRF_TRUSTED_ORIGINS = [
    o.strip() for o in os.environ.get('CSRF_TRUSTED_ORIGINS', '').split(',') if o.strip()
]

INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'django.contrib.humanize',
    'widget_tweaks',
    'axes',   # bloquea a quien prueba muchas contraseñas

    # ---- Apps nuevas de ICON LTDA ----
    'flota',
    'rutas',
    'gps',
    'inspeccion',
    'desplazamientos',
    'documentos',
]

# Sin AUTH_USER_MODEL: usamos el User por defecto de Django (django.contrib.auth.models.User).

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'whitenoise.middleware.WhiteNoiseMiddleware',   # DESPLIEGUE: sirve los estáticos; va justo después de Security
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
    'axes.middleware.AxesMiddleware',   # siempre de último
]

ROOT_URLCONF = 'sena.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [os.path.join(BASE_DIR, 'templates')],
        'APP_DIRS': False,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
                'rutas.alertas.alertas_documentos',
            ],
            'loaders': [
                'django.template.loaders.filesystem.Loader',
                'django.template.loaders.app_directories.Loader',
            ],
        },
    },
]

WSGI_APPLICATION = 'sena.wsgi.application'

# ========== BASE DE DATOS ==========
# DESPLIEGUE: si existe DATABASE_URL (Render) se usa Postgres; si no, sigue tu MySQL local.
# En el MySQL local no hay valor por defecto para usuario ni contraseña: si faltan en
# el .env, falla en vez de entrar como root o con una clave quemada.
# DB_USER debe ser un usuario propio (icon_app), con permisos solo sobre esta base.
if os.environ.get('DATABASE_URL'):
    # conn_health_checks: si Neon durmió la conexión, Django reconecta en vez de fallar.
    DATABASES = {'default': dj_database_url.config(conn_max_age=600, conn_health_checks=True)}
else:
    DATABASES = {
        'default': {
            'ENGINE':   'django.db.backends.mysql',
            'NAME':     os.environ.get('DB_NAME', 'icon'),
            'USER':     os.environ['DB_USER'],
            'PASSWORD': os.environ['DB_PASSWORD'],
            'HOST':     os.environ.get('DB_HOST', 'localhost'),
            'PORT':     os.environ.get('DB_PORT', '3306'),
            'OPTIONS': {
                'charset': 'utf8mb4',
                'init_command': "SET sql_mode='STRICT_TRANS_TABLES'",
            },
        }
    }

# Contraseñas de mínimo 10 caracteres
AUTH_PASSWORD_VALIDATORS = [
    {'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator'},
    {'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator',
     'OPTIONS': {'min_length': 10}},
    {'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator'},
    {'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator'},
]

# Los enlaces de recuperación de contraseña duran 1 hora (antes 3 días).
PASSWORD_RESET_TIMEOUT = 60 * 60

# Quién puede intentar entrar y cuántas veces ------------------------------
AUTHENTICATION_BACKENDS = [
    'axes.backends.AxesStandaloneBackend',        # primero: revisa los bloqueos
    'django.contrib.auth.backends.ModelBackend',
]
AXES_FAILURE_LIMIT = 5                 # intentos fallidos antes de bloquear
AXES_COOLOFF_TIME = timedelta(hours=1) # tiempo de bloqueo
AXES_RESET_ON_SUCCESS = True           # un acceso correcto reinicia el contador

# Detrás de un proxy, axes debe leer la IP real del visitante. Sin esto, todos
# llegan con la misma IP y 5 intentos fallidos de cualquiera bloquearían a todos.
# Render envía X-Forwarded-For. Si tras desplegar un solo intento fallido bloquea
# a todos, ajusta AXES_IPWARE_PROXY_COUNT.
# (Solo se activa con BEHIND_PROXY=True para que nadie pueda falsificar la cabecera.)
if BEHIND_PROXY:
    AXES_IPWARE_PROXY_COUNT = 1
    AXES_IPWARE_META_PRECEDENCE_ORDER = ['HTTP_X_FORWARDED_FOR']

LANGUAGE_CODE = 'es-co'
TIME_ZONE = 'America/Bogota'
USE_I18N = True
USE_TZ = True
USE_THOUSAND_SEPARATOR = True

STATIC_URL = '/static/'
STATICFILES_DIRS = [os.path.join(BASE_DIR, 'static')]
STATIC_ROOT = os.path.join(BASE_DIR, 'staticfiles')

# DESPLIEGUE: WhiteNoise sirve los archivos estáticos desde la misma app.
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "whitenoise.storage.CompressedStaticFilesStorage"},
}

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

# ========== REDIRECCION LOGIN/LOGOUT ==========
# Nombres de URL (no rutas fijas): 'panel' viene de gps/urls.py, 'login' de sena/urls.py
LOGIN_URL = 'login'
LOGIN_REDIRECT_URL = 'panel'
LOGOUT_REDIRECT_URL = 'login'

# Seguridad de la sesión y cabeceras ------------------------------------------------
SESSION_COOKIE_HTTPONLY = True          # JavaScript no puede leer la cookie de sesión
SESSION_COOKIE_SAMESITE = 'Lax'
SESSION_COOKIE_AGE = 60 * 60 * 24 * 7   # con "Recordarme": 7 días
CSRF_COOKIE_SAMESITE = 'Lax'
SECURE_CONTENT_TYPE_NOSNIFF = True
X_FRAME_OPTIONS = 'DENY'                # nadie puede meter tu sitio en un iframe
SECURE_REFERRER_POLICY = 'same-origin'  # no filtra tus URLs a otros sitios

# Solo cuando DEBUG=False (servidor real con HTTPS) ---------------------------
if not DEBUG:
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    SECURE_SSL_REDIRECT = True
    SECURE_HSTS_SECONDS = 60 * 60 * 24 * 30   # 30 días; súbelo cuando todo funcione
    SECURE_HSTS_INCLUDE_SUBDOMAINS = True

    # Si Django corre detrás de un proxy (Render), activa BEHIND_PROXY=True.
    # Sin esto, SECURE_SSL_REDIRECT causa un bucle de redirecciones.
    if BEHIND_PROXY:
        SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')

# ========== MESSAGES + SWEETALERT2 ==========
MESSAGE_TAGS = {
    messages.DEBUG:   'info',
    messages.INFO:    'info',
    messages.SUCCESS: 'success',
    messages.WARNING: 'warning',
    messages.ERROR:   'error',
}

MEDIA_URL = '/media/'
MEDIA_ROOT = os.path.join(BASE_DIR, 'media')

# Carpeta para archivos sensibles (reportes generados, Excel de FILPAC).
# Está FUERA de MEDIA_ROOT, así que no se sirve por URL: solo se entrega
# mediante vistas con login. Agrega  privado/  al .gitignore.
# OJO: en Render el disco es temporal; lo guardado aquí se pierde en cada redeploy.
PRIVATE_ROOT = BASE_DIR / 'privado'

# ========== CORREO GMAIL ==========
EMAIL_BACKEND       = 'django.core.mail.backends.smtp.EmailBackend'
EMAIL_HOST          = 'smtp.gmail.com'
EMAIL_PORT          = 587
EMAIL_USE_TLS       = True
EMAIL_TIMEOUT       = 10   # si Gmail no responde, no deja colgado el servidor
EMAIL_HOST_USER     = os.environ['EMAIL_HOST_USER']
EMAIL_HOST_PASSWORD = os.environ['EMAIL_HOST_PASSWORD']
DEFAULT_FROM_EMAIL  = 'ICON LTDA <iconltda18@gmail.com>'

# ========== INDICADOR PESV DE VELOCIDAD (SIG.FT-57) ==========
PESV_LIMITE_VELOCIDAD = 80  # km/h; trayectos con velocidad máxima por encima cuentan como exceso
PESV_PLANTILLA_VELOCIDAD = BASE_DIR / "plantillas" / "INDICADOR_PESV_VELOCIDAD.xlsx"

# ========== LOGGING (para ver avisos en los Logs de Render) ==========
LOGGING = {
    'version': 1,
    'disable_existing_loggers': False,
    'handlers': {'console': {'class': 'logging.StreamHandler'}},
    'root': {'handlers': ['console'], 'level': 'WARNING'},
}