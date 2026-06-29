"""Unit tests for the IsOwner object-level permission backstop.

End-to-end ownership scoping (foreign objects → 404) is covered in
test_sessions_api.py; here we exercise the permission in isolation.
"""

from __future__ import annotations

import pytest

from apps.diagnostics.permissions import IsOwner

from .factories import (
    DebugSessionFactory,
    DiagnosisReportFactory,
    ProjectFactory,
    UploadedFileFactory,
)

pytestmark = pytest.mark.django_db


class _FakeRequest:
    """Minimal stand-in carrying just the acting user."""

    def __init__(self, user):
        self.user = user


@pytest.fixture
def check():
    return IsOwner().has_object_permission


@pytest.mark.parametrize(
    "make_obj",
    [
        lambda user: ProjectFactory(user=user),
        lambda user: DebugSessionFactory(project__user=user),
        lambda user: UploadedFileFactory(debug_session__project__user=user),
        lambda user: DiagnosisReportFactory(debug_session__project__user=user),
    ],
)
def test_owner_is_granted(check, user, make_obj):
    obj = make_obj(user)
    assert check(_FakeRequest(user), None, obj) is True


@pytest.mark.parametrize(
    "make_obj",
    [
        lambda user: ProjectFactory(user=user),
        lambda user: DebugSessionFactory(project__user=user),
        lambda user: UploadedFileFactory(debug_session__project__user=user),
        lambda user: DiagnosisReportFactory(debug_session__project__user=user),
    ],
)
def test_non_owner_is_denied(check, user, other_user, make_obj):
    obj = make_obj(user)
    assert check(_FakeRequest(other_user), None, obj) is False
