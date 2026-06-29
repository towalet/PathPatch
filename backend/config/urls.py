"""Root URL configuration for PatchPath."""

from __future__ import annotations

from django.contrib import admin
from django.urls import include, path

from .health import health_check

api_patterns = [
    path("health/", health_check, name="health"),
    path("auth/", include("apps.accounts.urls")),
    path("", include("apps.diagnostics.urls")),
]

urlpatterns = [
    path("admin/", admin.site.urls),
    path("api/", include((api_patterns, "api"))),
]
