"""Shared pytest fixtures for the PatchPath backend test suite."""

from __future__ import annotations

import pytest
from rest_framework.test import APIClient

from apps.diagnostics.tests.factories import UserFactory


@pytest.fixture
def api_client() -> APIClient:
    """An unauthenticated DRF API client."""
    return APIClient()


@pytest.fixture
def user(db):
    """A persisted user (the default 'acting' owner in tests)."""
    return UserFactory()


@pytest.fixture
def other_user(db):
    """A second user, used to assert cross-tenant isolation."""
    return UserFactory()


@pytest.fixture
def auth_client(user) -> APIClient:
    """An API client authenticated as ``user`` (its own client instance)."""
    client = APIClient()
    client.force_authenticate(user=user)
    return client


@pytest.fixture
def as_user():
    """Factory fixture: return an API client authenticated as a given user."""

    def _make(account) -> APIClient:
        client = APIClient()
        client.force_authenticate(user=account)
        return client

    return _make
