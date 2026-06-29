"""Settings used by the pytest suite.

Fast password hashing, an in-process email backend, and a deterministic AI
configuration so tests never reach the network. The AI client is always mocked
in tests (see apps/diagnostics/tests).
"""

from __future__ import annotations

from .base import *  # noqa: F401,F403

DEBUG = False

# Speed up password hashing in tests.
PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]

EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"

# Disable throttling noise in unit tests.
REST_FRAMEWORK["DEFAULT_THROTTLE_CLASSES"] = ()  # noqa: F405
REST_FRAMEWORK["DEFAULT_THROTTLE_RATES"] = {}  # noqa: F405

# Never use a real key during tests.
PATCHPATH_AI["API_KEY"] = "test-key"  # noqa: F405
PATCHPATH_AI["MODEL"] = "test-model"  # noqa: F405
