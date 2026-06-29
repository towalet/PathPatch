"""Phase 8 demo seed coverage.

The recruiter demo depends on `manage.py seed_demo` creating a complete,
offline-safe diagnosis. This test keeps the command idempotent and verifies the
objects needed by the UI are present.
"""

from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model
from django.core.management import call_command

from apps.diagnostics.management.commands.seed_demo import (
    DEMO_EMAIL,
    DEMO_PASSWORD,
    DEMO_PROJECT_NAME,
)
from apps.diagnostics.models import DebugSession, DiagnosisReport, Project, SessionStatus


@pytest.mark.django_db
def test_seed_demo_creates_completed_demo_report():
    call_command("seed_demo")

    user = get_user_model().objects.get(email=DEMO_EMAIL)
    assert user.check_password(DEMO_PASSWORD)

    project = Project.objects.get(user=user, name=DEMO_PROJECT_NAME)
    session = DebugSession.objects.get(project=project)
    report = DiagnosisReport.objects.get(debug_session=session)

    assert session.status == SessionStatus.COMPLETED
    assert session.files.count() == 3
    assert session.detected_issues.filter(issue_type="missing_database_url").exists()
    assert report.confidence_score < 1
    assert report.evidence_json
    assert report.missing_information_json


@pytest.mark.django_db
def test_seed_demo_is_idempotent():
    call_command("seed_demo")
    call_command("seed_demo")

    user = get_user_model().objects.get(email=DEMO_EMAIL)
    project = Project.objects.get(user=user, name=DEMO_PROJECT_NAME)
    session = DebugSession.objects.get(project=project)

    assert Project.objects.filter(user=user, name=DEMO_PROJECT_NAME).count() == 1
    assert DebugSession.objects.filter(project=project).count() == 1
    assert session.files.count() == 3
    assert DiagnosisReport.objects.filter(debug_session=session).count() == 1
