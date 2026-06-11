"""
Django settings for backend project.
"""

from pathlib import Path
from corsheaders.defaults import default_headers
from dotenv import load_dotenv
import os

# =========================
# BASE DIR + ENV
# =========================

BASE_DIR = Path(__file__).resolve().parent.parent

# Load .env file from same folder as manage.py
load_dotenv(BASE_DIR / ".env")

# =========================
# SECURITY
# =========================

SECRET_KEY = os.getenv(
    "SECRET_KEY",
    "django-insecure-m_7@wsv7@k6!rva-ieibzii6dmb#*o@43yiaiz$#-gabbv##h)",
)

DEBUG = os.getenv("DEBUG", "true").lower() == "true"

ALLOWED_HOSTS = ["*"]

# =========================
# APPS
# =========================

INSTALLED_APPS = [
    "daphne",

    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",

    # Third party apps
    "corsheaders",
    "rest_framework",
    "rest_framework_simplejwt",
    "channels",

    # Local apps
    "accounts",
    "chats",
]

# =========================
# MIDDLEWARE
# =========================

MIDDLEWARE = [
    "corsheaders.middleware.CorsMiddleware",

    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

# =========================
# URLS / ASGI / WSGI
# =========================

ROOT_URLCONF = "backend.urls"

ASGI_APPLICATION = "backend.asgi.application"

WSGI_APPLICATION = "backend.wsgi.application"

# =========================
# TEMPLATES
# =========================

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

# =========================
# USER MODEL
# =========================

AUTH_USER_MODEL = "accounts.User"

# =========================
# DATABASE
# =========================

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": BASE_DIR / "db.sqlite3",
    }
}

# =========================
# REST FRAMEWORK
# =========================

REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": (
        "rest_framework_simplejwt.authentication.JWTAuthentication",
    ),
}

# =========================
# CORS
# =========================

CORS_ALLOW_ALL_ORIGINS = True

CORS_ALLOW_HEADERS = list(default_headers) + [
    "authorization",
]

# =========================
# CSRF
# =========================

# For local API/mobile testing, this can stay commented.
# Add trusted origins only when using browser frontend with CSRF/session auth.
#
# CSRF_TRUSTED_ORIGINS = [
#     "http://localhost:8080",
#     "http://192.168.1.64:8080",
#     "http://127.0.0.1:3000",
#     "http://localhost:5173",
#     "http://127.0.0.1:5173",
# ]

# =========================
# PASSWORD VALIDATION
# =========================

AUTH_PASSWORD_VALIDATORS = [
    {
        "NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.CommonPasswordValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.NumericPasswordValidator",
    },
]

# =========================
# CHANNELS
# =========================

CHANNEL_LAYERS = {
    "default": {
        "BACKEND": "channels.layers.InMemoryChannelLayer",
    }
}

# =========================
# MEDIA
# =========================

MEDIA_URL = "/media/"
MEDIA_ROOT = BASE_DIR / "media"

# =========================
# STATIC
# =========================

STATIC_URL = "/static/"
STATIC_ROOT = BASE_DIR / "staticfiles"

# This avoids staticfiles.W004 warning if static folder does not exist.
STATICFILES_DIRS = []

STATIC_DIR = BASE_DIR / "static"
if STATIC_DIR.exists():
    STATICFILES_DIRS.append(STATIC_DIR)

# =========================
# INTERNATIONALIZATION
# =========================

LANGUAGE_CODE = "en-us"

TIME_ZONE = "UTC"

USE_I18N = True

USE_TZ = True

# =========================
# DEFAULT AUTO FIELD
# =========================

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# =========================
# FIREBASE
# =========================

FIREBASE_CREDENTIALS = BASE_DIR / "chats/firebase_chat.json"

# =========================
# TWILIO
# =========================

TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID", "").strip()
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN", "").strip()
TWILIO_PHONE_NUMBER = os.getenv("TWILIO_PHONE_NUMBER", "").strip()

# Default false is safer.
# If credentials are missing, API will not break; OTP prints in terminal.
TWILIO_ENABLE_SMS = os.getenv("TWILIO_ENABLE_SMS", "false").lower() == "true"