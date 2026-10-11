"""Shared settings for the TGCG Election Monitoring & Collation backend.

Environment-specific modules (dev, test, prod) import from here. Values come
from environment variables or a local `.env` file (see `.env.example`).
"""

from datetime import timedelta
from pathlib import Path

import environ

BASE_DIR = Path(__file__).resolve().parent.parent.parent

env = environ.Env()
environ.Env.read_env(BASE_DIR / ".env", overwrite=False)

SECRET_KEY = env("DJANGO_SECRET_KEY")
DEBUG = env.bool("DJANGO_DEBUG", default=False)
ALLOWED_HOSTS = env.list("DJANGO_ALLOWED_HOSTS", default=[])

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    # Third party
    "corsheaders",
    "rest_framework",
    "rest_framework_simplejwt",
    "rest_framework_simplejwt.token_blacklist",
    "django_filters",
    "drf_spectacular",
    # TGCG
    "apps.core",
    "apps.geography",
    "apps.accounts",
    "apps.membership",
    "apps.field",
    "apps.results",
    "apps.announcements",
    "apps.tracking",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "corsheaders.middleware.CorsMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"

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

WSGI_APPLICATION = "config.wsgi.application"
ASGI_APPLICATION = "config.asgi.application"

# PostgreSQL in every shared environment; SQLite is only a local fallback.
DATABASES = {
    "default": env.db(
        "DATABASE_URL",
        default=f"sqlite:///{BASE_DIR / 'db.sqlite3'}",
    ),
}
DATABASES["default"]["ATOMIC_REQUESTS"] = True

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

AUTH_USER_MODEL = "accounts.User"
AUTHENTICATION_BACKENDS = [
    "apps.accounts.backends.AccessIdOrPhoneBackend",
]

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {
        "NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
        "OPTIONS": {"min_length": 10},
    },
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "en-us"
TIME_ZONE = "Africa/Lagos"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
MEDIA_URL = "media/"
MEDIA_ROOT = BASE_DIR / "media"

# --- API -------------------------------------------------------------------
REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "rest_framework_simplejwt.authentication.JWTAuthentication",
    ],
    "DEFAULT_PERMISSION_CLASSES": [
        "rest_framework.permissions.IsAuthenticated",
    ],
    "DEFAULT_FILTER_BACKENDS": [
        "django_filters.rest_framework.DjangoFilterBackend",
        "rest_framework.filters.SearchFilter",
        "rest_framework.filters.OrderingFilter",
    ],
    "DEFAULT_PAGINATION_CLASS": "rest_framework.pagination.LimitOffsetPagination",
    "PAGE_SIZE": 100,
    "DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema",
    "EXCEPTION_HANDLER": "apps.core.exceptions.exception_handler",
    "DEFAULT_THROTTLE_CLASSES": [
        "rest_framework.throttling.AnonRateThrottle",
        "rest_framework.throttling.UserRateThrottle",
    ],
    "DEFAULT_THROTTLE_RATES": {
        "anon": env("API_THROTTLE_ANON", default="60/min"),
        "user": env("API_THROTTLE_USER", default="600/min"),
        "login": env("API_THROTTLE_LOGIN", default="10/min"),
        "registration": env("API_THROTTLE_REGISTRATION", default="30/hour"),
        "delim_lookup": env("API_THROTTLE_DELIM_LOOKUP", default="120/hour"),
    },
}

SIMPLE_JWT = {
    "ACCESS_TOKEN_LIFETIME": timedelta(
        minutes=env.int("JWT_ACCESS_MINUTES", default=15),
    ),
    "REFRESH_TOKEN_LIFETIME": timedelta(
        days=env.int("JWT_REFRESH_DAYS", default=7),
    ),
    "ROTATE_REFRESH_TOKENS": True,
    "UPDATE_LAST_LOGIN": True,
}

SPECTACULAR_SETTINGS = {
    "TITLE": "TGCG-EMCOP API",
    "DESCRIPTION": "The Game Changer Group Election Monitoring & Collation Programme.",
    "VERSION": "0.1.0",
    "SERVE_INCLUDE_SCHEMA": False,
    "COMPONENT_SPLIT_REQUEST": True,
    "ENUM_NAME_OVERRIDES": {
        "AccreditationStatus": "apps.core.choices.AccreditationStatus",
        "RecordStatus": "apps.core.choices.RecordStatus",
        "RecordOrigin": "apps.core.choices.RecordOrigin",
        "Role": "apps.core.choices.Role",
        "GeographyLevel": "apps.core.choices.GeographyLevel",
        "IncidentStatus": "apps.core.choices.IncidentStatus",
        "IncidentSeverity": "apps.core.choices.IncidentSeverity",
        "IncidentCategory": "apps.core.choices.IncidentCategory",
        "ReportCategory": "apps.core.choices.ReportCategory",
        "EvidenceType": "apps.core.choices.EvidenceType",
    },
}

CORS_ALLOWED_ORIGINS = env.list("CORS_ALLOWED_ORIGINS", default=[])

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {"console": {"class": "logging.StreamHandler"}},
    "root": {"handlers": ["console"], "level": env("LOG_LEVEL", default="INFO")},
}

# --- Evidence uploads --------------------------------------------------------
# Evidence is never served from MEDIA_URL; downloads go through an
# authenticated, scope-checked endpoint. Use private object storage in prod.
EVIDENCE_MAX_UPLOAD_MB = env.int("EVIDENCE_MAX_UPLOAD_MB", default=25)
EVIDENCE_MAX_VIDEO_MB = env.int("EVIDENCE_MAX_VIDEO_MB", default=150)
FILE_UPLOAD_MAX_MEMORY_SIZE = 5 * 1024 * 1024

# --- Login protection ----------------------------------------------------------
LOGIN_MAX_FAILED_ATTEMPTS = env.int("LOGIN_MAX_FAILED_ATTEMPTS", default=5)
LOGIN_LOCKOUT_MINUTES = env.int("LOGIN_LOCKOUT_MINUTES", default=15)

# --- SMS (phone verification codes) ---------------------------------------------
# "console" logs codes (development only); "termii" sends real SMS.
SMS_BACKEND = env("SMS_BACKEND", default="console")
TERMII_API_KEY = env("TERMII_API_KEY", default="")
TERMII_SENDER_ID = env("TERMII_SENDER_ID", default="TGCG")
TERMII_BASE_URL = env("TERMII_BASE_URL", default="https://api.ng.termii.com")
OTP_TTL_MINUTES = env.int("OTP_TTL_MINUTES", default=10)
OTP_MAX_ATTEMPTS = 5
OTP_RESEND_SECONDS = 60
OTP_MAX_PER_HOUR = 5

# --- Member registration ---------------------------------------------------------
MEMBER_PHOTO_MAX_MB = env.int("MEMBER_PHOTO_MAX_MB", default=8)

# --- GPS ---------------------------------------------------------------------------
PU_PROXIMITY_METERS = env.int("PU_PROXIMITY_METERS", default=300)
GPS_MAX_ACCURACY_M = env.int("GPS_MAX_ACCURACY_M", default=150)
LIVE_STALE_MINUTES = env.int("LIVE_STALE_MINUTES", default=15)
LOCATION_PING_RETENTION_DAYS = env.int("LOCATION_PING_RETENTION_DAYS", default=30)

# --- Polling unit mapping --------------------------------------------------------
PIN_MAX_ACCURACY_M = env.int("PIN_MAX_ACCURACY_M", default=30)
PIN_CONFIRM_RADIUS_M = env.int("PIN_CONFIRM_RADIUS_M", default=50)
