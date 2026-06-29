"""Auth URL routes mounted under /api/auth/."""

from __future__ import annotations

from django.urls import path

from .views import LoginView, MeView, RefreshView, RegisterView

app_name = "accounts"

urlpatterns = [
    path("register/", RegisterView.as_view(), name="register"),
    path("login/", LoginView.as_view(), name="login"),
    path("refresh/", RefreshView.as_view(), name="refresh"),
    path("me/", MeView.as_view(), name="me"),
]
