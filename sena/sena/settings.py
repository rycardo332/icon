import os
from pathlib import Path
from django.contrib.messages import constants as messages
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent

# ========== CARGA DE VARIABLES DE ENTORNO (.env) ==========
load_dotenv(BASE_DIR / '.env')

SECRET_KEY = os.environ.get('SECRET_KEY', 'django-insecure-(1x7w0^_8zvx$7z$@4z!j+31!r0@=13vs0@qby2-ac8xuj#c=u')
DEBUG = os.environ.get('DEBUG', 'True') == 'True'
ALLOWED_HOSTS = ['localhost', '127.0.0.1', '0.0.0.0']

INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'django.contrib.humanize',
    'widget_tweaks',

    # ---- Apps nuevas de ICON LTDA ----
    'flota',
    'rutas',
    'gps',
    'inspeccion',
    'desplazamientos',
    'documentos',

    # ---- Restos de Acerautos: pendientes de decidir ----
    # 'app',      # Acerautos original — déjala fuera hasta que confirmes que no queda nada útil
    # 'login',    # ya no hace falta: usamos el login integrado de Django (ver LOGIN_URL abajo)
    # 'usuario',  # PerfilUsuario custom — ya NO se necesita, el User por defecto de Django alcanza
]

# Sin AUTH_USER_MODEL: usamos el User por defecto de Django (django.contrib.auth.models.User).
# Ya no hace falta ningún modelo de usuario propio.

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
    # 'app.middleware.AlertasAutomaticasMiddleware',  # depende de 'app' (Acerautos) — reactivar solo si la adaptas a ICON LTDA
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
            ],
            'loaders': [
                'django.template.loaders.filesystem.Loader',
                'django.template.loaders.app_directories.Loader',
            ],
        },
    },
]

WSGI_APPLICATION = 'sena.wsgi.application'

# ========== BASE DE DATOS MySQL ==========
# IMPORTANTE: sin valores por defecto para credenciales reales.
# Si falta el .env, esto debe fallar en vez de usar una contraseña quemada en el código.
DATABASES = {
    'default': {
        'ENGINE':   'django.db.backends.mysql',
        'NAME':     os.environ.get('DB_NAME', 'icon'),
        'USER':     os.environ.get('DB_USER', 'root'),
        'PASSWORD': os.environ['DB_PASSWORD'],
        'HOST':     os.environ.get('DB_HOST', 'localhost'),
        'PORT':     os.environ.get('DB_PORT', '3306'),
        'OPTIONS': {
            'charset': 'utf8mb4',
            'init_command': "SET sql_mode='STRICT_TRANS_TABLES'",
        },
    }
}

AUTH_PASSWORD_VALIDATORS = [
    {'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator'},
    {'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator'},
    {'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator'},
    {'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator'},
]

LANGUAGE_CODE = 'es-co'
TIME_ZONE = 'America/Bogota'
USE_I18N = True
USE_TZ = True
USE_THOUSAND_SEPARATOR = True

STATIC_URL = '/static/'
STATICFILES_DIRS = [os.path.join(BASE_DIR, 'static')]
STATIC_ROOT = os.path.join(BASE_DIR, 'staticfiles')
DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

# ========== REDIRECCION LOGIN/LOGOUT ==========
LOGIN_REDIRECT_URL = '/principal/dashboard/'
LOGOUT_REDIRECT_URL = '/'
LOGIN_URL = '/login/'

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

# ========== CORREO GMAIL ==========
# IMPORTANTE: sin valores por defecto para credenciales reales (ver nota de seguridad abajo).
EMAIL_BACKEND       = 'django.core.mail.backends.smtp.EmailBackend'
EMAIL_HOST          = 'smtp.gmail.com'
EMAIL_PORT          = 587
EMAIL_USE_TLS       = True
EMAIL_HOST_USER     = os.environ['EMAIL_HOST_USER']
EMAIL_HOST_PASSWORD = os.environ['EMAIL_HOST_PASSWORD']
DEFAULT_FROM_EMAIL  = 'ICON LTDA <iconltda18@gmail.com>'