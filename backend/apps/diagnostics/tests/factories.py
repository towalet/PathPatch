"""
factory_boy factories for the diagnostics domain.

Each factory wires its own ownership chain via ``SubFactory`` so a single
``DiagnosisReportFactory()`` call produces a full user → project → session →
report graph. JSON fields use ``LazyFunction`` to avoid shared mutable defaults.
"""

from __future__ import annotations

import factory
from django.contrib.auth import get_user_model

from apps.diagnostics.models import (
    DebugSession,
    DetectedIssue,
    DiagnosisReport,
    Project,
    SessionStatus,
    Severity,
    UploadedFile,
)

User = get_user_model()


class UserFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = User
        django_get_or_create = ("email",)

    email = factory.Sequence(lambda n: f"user{n}@example.com")
    name = factory.Faker("name")
    password = factory.PostGenerationMethodCall("set_password", "testpass1234")


class ProjectFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = Project

    user = factory.SubFactory(UserFactory)
    name = factory.Sequence(lambda n: f"Project {n}")
    stack = "Django, PostgreSQL"
    cloud_provider = "Render"


class DebugSessionFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = DebugSession

    project = factory.SubFactory(ProjectFactory)
    status = SessionStatus.PENDING
    error_summary = factory.Faker("sentence")


class UploadedFileFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = UploadedFile

    debug_session = factory.SubFactory(DebugSessionFactory)
    filename = factory.Sequence(lambda n: f"deploy-{n}.log")
    file_type = "log"
    content = "redacted log content"
    # Unique 64-char hex digest per instance (satisfies the per-session unique constraint).
    content_sha256 = factory.Sequence(lambda n: f"{n:064x}")
    size_bytes = 100
    line_count = 5
    redaction_count = 0


class DetectedIssueFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = DetectedIssue

    debug_session = factory.SubFactory(DebugSessionFactory)
    issue_type = "missing_database_url"
    severity = Severity.HIGH
    confidence_hint = 0.82
    matched_pattern = "DATABASE_URL"
    evidence = factory.LazyFunction(
        lambda: [
            {
                "source": "deploy.log",
                "line_or_section": "line 42",
                "snippet": "KeyError: DATABASE_URL",
                "reason": "Startup failed reading DATABASE_URL.",
            }
        ]
    )


class DiagnosisReportFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = DiagnosisReport

    debug_session = factory.SubFactory(DebugSessionFactory)
    root_cause = "Most likely the app started without DATABASE_URL set."
    confidence_score = 0.8
    severity = Severity.HIGH
    detected_stack = factory.LazyFunction(lambda: ["Django", "PostgreSQL"])
    detected_cloud_provider = "Render"
    explanation = "Based on uploaded evidence, DATABASE_URL was missing at boot."
    evidence_json = factory.LazyFunction(
        lambda: [
            {
                "source": "deploy.log",
                "line_or_section": "line 42",
                "reason": "Log shows DATABASE_URL was missing at startup.",
            }
        ]
    )
    recommended_fix = "Set DATABASE_URL in the service environment and redeploy."
    commands_json = factory.LazyFunction(lambda: ["render env:set DATABASE_URL=..."])
    verification_checklist_json = factory.LazyFunction(
        lambda: ["Confirm the service boots without a KeyError."]
    )
    missing_information_json = factory.LazyFunction(lambda: ["settings.py was not uploaded."])
    possible_risks_json = factory.LazyFunction(list)
    model_name = "test-model"
