"""Settings for the automated test suite."""

import os

os.environ.setdefault("DJANGO_SECRET_KEY", "test-only-secret-key-not-for-production")

from .base import *  # noqa: E402,F403
from .base import BASE_DIR, env  # noqa: E402

DATABASES = {
    "default": env.db(
        "TEST_DATABASE_URL",
        default=f"sqlite:///{BASE_DIR / 'test.sqlite3'}",
    ),
}
DATABASES["default"]["ATOMIC_REQUESTS"] = True

PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]

REST_FRAMEWORK["DEFAULT_THROTTLE_CLASSES"] = []  # noqa: F405
REST_FRAMEWORK["DEFAULT_THROTTLE_RATES"] = {  # noqa: F405
    scope: "100000/min"
    for scope in REST_FRAMEWORK["DEFAULT_THROTTLE_RATES"]  # noqa: F405
}
SMS_BACKEND = "console"
# Tests see every change at once; apps/core/tests/test_caching.py turns sharing on.
SUMMARY_CACHE_SECONDS = 0
# A server's .env may hold a real key: tests never send a photo to Google.
GOOGLE_VISION_API_KEY = ""
# Codes go to the console here, so joining needs one unless a test turns this off.
REGISTRATION_PHONE_CODE_REQUIRED = True
