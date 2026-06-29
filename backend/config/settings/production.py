"""
Production settings.

Validates required environment at import time so a misconfigured deploy fails
fast instead of serving with insecure defaults.
"""

from __future__ import annotations

from django.core.exceptions import ImproperlyConfigured

from .base import *  # noqa: F401,F403
from .base import env

DEBUG = False

# --- Required environment ---------------------------------------------------
SECRET_KEY = env.str("DJANGO_SECRET_KEY", default="")
if not SECRET_KEY or SECRET_KEY == "insecure-dev-key-change-me":
    raise ImproperlyConfigured(
        "DJANGO_SECRET_KEY must be set to a strong unique value in production."
    )

ALLOWED_HOSTS = env.list("DJANGO_ALLOWED_HOSTS", default=[])
if not ALLOWED_HOSTS:
    raise ImproperlyConfigured("DJANGO_ALLOWED_HOSTS must be set in production.")

if not PATCHPATH_AI["API_KEY"]:  # noqa: F405
    raise ImproperlyConfigured("OPENAI_API_KEY must be set in production for AI diagnosis.")

# --- HTTPS / transport security ---------------------------------------------
SECURE_SSL_REDIRECT = env.bool("SECURE_SSL_REDIRECT", default=True)
# Honour the proxy's forwarded-proto header (most PaaS terminate TLS upstream).
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")

SECURE_HSTS_SECONDS = env.int("SECURE_HSTS_SECONDS", default=31_536_000)  # 1 year
SECURE_HSTS_INCLUDE_SUBDOMAINS = True
SECURE_HSTS_PRELOAD = True
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = "same-origin"
X_FRAME_OPTIONS = "DENY"

# --- Cookies ----------------------------------------------------------------
SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
CSRF_COOKIE_SAMESITE = "Lax"
CSRF_TRUSTED_ORIGINS = env.list("CSRF_TRUSTED_ORIGINS", default=[])

# --- CORS -------------------------------------------------------------------
CORS_ALLOW_ALL_ORIGINS = False
CORS_ALLOWED_ORIGINS = env.list("CORS_ALLOWED_ORIGINS", default=[])
