"""Local development settings."""

from __future__ import annotations

from .base import *  # noqa: F401,F403
from .base import env

DEBUG = True

ALLOWED_HOSTS = ["localhost", "127.0.0.1", "0.0.0.0", "backend"]

# Surface email in the console during local development.
EMAIL_BACKEND = "django.core.mail.backends.console.EmailBackend"

# Be permissive with CORS only in development.
CORS_ALLOW_ALL_ORIGINS = env.bool("CORS_ALLOW_ALL_ORIGINS", default=True)
