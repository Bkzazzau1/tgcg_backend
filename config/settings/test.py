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
