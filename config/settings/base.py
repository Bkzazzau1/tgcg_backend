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
    "apps.meetings",
    "apps.forum",
    "apps.access",
    "apps.wallets",
    "apps.projects",
    "apps.messaging",
    "apps.security",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "corsheaders.middleware.CorsMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "apps.access.middleware.AccessCacheMiddleware",
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
        "pvc_reading": env("API_THROTTLE_PVC_READING", default="30/hour"),
        # Public INEC lists for finding a polling unit by name; several per sign-up.
        "registration_places": env("API_THROTTLE_REGISTRATION_PLACES", default="600/hour"),
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
        "PermissionCode": "apps.core.permissions.Capability",
        "RoleCategory": "apps.access.models.RoleCategory",
        "AuthoritySource": "apps.access.models.AuthoritySource",
        "SubmissionSource": "apps.core.choices.SubmissionSource",
        "AdminActionKind": "apps.access.models.AdminActionKind",
        "VerificationCheckKind": "apps.membership.models.VerificationCheck.Kind",
        "VerificationCheckResult": "apps.membership.models.VerificationCheck.Result",
        "GeographyLevel": "apps.core.choices.GeographyLevel",
        "IncidentStatus": "apps.core.choices.IncidentStatus",
        "IncidentSeverity": "apps.core.choices.IncidentSeverity",
        "IncidentCategory": "apps.core.choices.IncidentCategory",
        "ReportCategory": "apps.core.choices.ReportCategory",
        "EvidenceType": "apps.core.choices.EvidenceType",
        "MeetingKind": "apps.meetings.models.Meeting.Kind",
        "MeetingAudience": "apps.meetings.models.Meeting.Audience",
        "MeetingStatus": "apps.meetings.models.Meeting.Status",
        "ForumPostStatus": "apps.forum.models.Post.Status",
        "ForumReplyStatus": "apps.forum.models.Reply.Status",
        "ForumAttachmentKind": "apps.forum.models.Attachment.Kind",
        "WalletPaymentStatus": "apps.wallets.models.Payment.Status",
        "WalletEntryKind": "apps.wallets.models.WalletEntry.Kind",
        "ParticipationStatus": "apps.projects.models.ParticipationStatus",
        "MessageChannel": "apps.messaging.models.Channel",
        "BroadcastStatus": "apps.messaging.models.Broadcast.Status",
        "SecurityDispatchStatus": "apps.security.models.SecurityDispatch.Status",
        "SecurityLocatedBy": "apps.security.models.SecurityDispatch.LocatedBy",
        "DeliveryStatus": "apps.messaging.models.Delivery.Status",
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

# --- Forum ---------------------------------------------------------------------
# Pictures and videos in posts, served only through the forum's own endpoint.
FORUM_MAX_PHOTO_MB = env.int("FORUM_MAX_PHOTO_MB", default=15)
FORUM_MAX_VIDEO_MB = env.int("FORUM_MAX_VIDEO_MB", default=50)
FORUM_MAX_ATTACHMENTS = env.int("FORUM_MAX_ATTACHMENTS", default=10)
# All of a post's files together; Nginx's client_max_body_size must allow it.
FORUM_MAX_POST_MB = env.int("FORUM_MAX_POST_MB", default=100)

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
# Joining in the app proves the phone with an SMS code only while SMS can be
# sent ("console" in development, or Termii with a key). Without SMS, members
# join at once, as on USSD, and the number stays unconfirmed (no
# phone_verified_at) until a code confirms it later, e.g. on a PIN reset.
REGISTRATION_PHONE_CODE_REQUIRED = env.bool(
    "REGISTRATION_PHONE_CODE_REQUIRED",
    default=SMS_BACKEND == "console" or bool(TERMII_API_KEY),
)
OTP_MAX_PER_HOUR = 5

# --- Bulk messages (apps.messaging) -----------------------------------------------
# SMS go through SMS_BACKEND above. "console" records messages without sending
# them (development and tests); "termii" and "fcm" send for real.
WHATSAPP_BACKEND = env("WHATSAPP_BACKEND", default="console")
PUSH_BACKEND = env("PUSH_BACKEND", default="console")
# Termii's route for bulk SMS: "generic" does not reach numbers on DND;
# "dnd" does, once Termii has approved the sender for it.
TERMII_BULK_CHANNEL = env("TERMII_BULK_CHANNEL", default="generic")
# A Firebase service account's JSON key file, kept outside the code (push).
FIREBASE_SERVICE_ACCOUNT_FILE = env("FIREBASE_SERVICE_ACCOUNT_FILE", default="")

# --- Member registration ---------------------------------------------------------
MEMBER_PHOTO_MAX_MB = env.int("MEMBER_PHOTO_MAX_MB", default=8)

# --- Reading PVC photos for browsers (see apps/membership/pvc_reading.py) ----------
# Google Cloud Vision when a key is set (the photo goes to Google); otherwise
# Tesseract, if installed; otherwise people type the details from the card.
GOOGLE_VISION_API_KEY = env("GOOGLE_VISION_API_KEY", default="")
TESSERACT_CMD = env("TESSERACT_CMD", default="tesseract")

# --- Joining by USSD (see apps/membership/ussd.py) ---------------------------------
# The provider posts to /api/v1/ussd/<USSD_CALLBACK_TOKEN>/; empty turns USSD off.
USSD_CALLBACK_TOKEN = env("USSD_CALLBACK_TOKEN", default="")
# The code people dial, e.g. *384*1234#. When set, other codes are refused.
USSD_SERVICE_CODE = env("USSD_SERVICE_CODE", default="")
# Inactive saved USSD progress is purged daily; membership is retained.
USSD_DRAFT_TTL_DAYS = env.int("USSD_DRAFT_TTL_DAYS", default=30)

# --- Meetings (see apps/meetings/livekit.py and docs/meetings.md) -------------------
# Calls run on LiveKit. All three empty: meetings can be planned, but not joined.
LIVEKIT_URL = env("LIVEKIT_URL", default="")  # wss://<project>.livekit.cloud
LIVEKIT_API_KEY = env("LIVEKIT_API_KEY", default="")
LIVEKIT_API_SECRET = env("LIVEKIT_API_SECRET", default="")

# Live incident video: a stream counts as live for at most this long, unless
# ended sooner. Recordings go to an S3-compatible bucket (LiveKit uploads
# there), and the server copies each onto its incident as evidence.
LIVE_STREAM_MAX_MINUTES = env.int("LIVE_STREAM_MAX_MINUTES", default=120)
LIVE_RECORDING_S3_ENDPOINT = env("LIVE_RECORDING_S3_ENDPOINT", default="")
LIVE_RECORDING_S3_BUCKET = env("LIVE_RECORDING_S3_BUCKET", default="")
LIVE_RECORDING_S3_REGION = env("LIVE_RECORDING_S3_REGION", default="us-east-1")
LIVE_RECORDING_S3_ACCESS_KEY = env("LIVE_RECORDING_S3_ACCESS_KEY", default="")
LIVE_RECORDING_S3_SECRET = env("LIVE_RECORDING_S3_SECRET", default="")
LIVE_RECORDING_S3_FORCE_PATH_STYLE = env.bool("LIVE_RECORDING_S3_FORCE_PATH_STYLE", default=True)

# --- GPS ---------------------------------------------------------------------------
PU_PROXIMITY_METERS = env.int("PU_PROXIMITY_METERS", default=300)
GPS_MAX_ACCURACY_M = env.int("GPS_MAX_ACCURACY_M", default=150)
LIVE_STALE_MINUTES = env.int("LIVE_STALE_MINUTES", default=15)
LOCATION_PING_RETENTION_DAYS = env.int("LOCATION_PING_RETENTION_DAYS", default=30)

# --- Polling unit mapping --------------------------------------------------------
PIN_MAX_ACCURACY_M = env.int("PIN_MAX_ACCURACY_M", default=30)
PIN_CONFIRM_RADIUS_M = env.int("PIN_CONFIRM_RADIUS_M", default=50)
