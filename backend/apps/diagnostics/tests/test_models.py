"""Model-level tests: constraints, ownership scoping, and queryset helpers."""

from __future__ import annotations

import pytest
from django.db import IntegrityError, transaction

from apps.diagnostics.models import (
    DetectedIssue,
    Project,
    SessionStatus,
    Severity,
)

from .factories import (
    DebugSessionFactory,
    DetectedIssueFactory,
    DiagnosisReportFactory,
    ProjectFactory,
    UploadedFileFactory,
)

pytestmark = pytest.mark.django_db


class TestProjectConstraints:
    def test_name_unique_per_user_case_insensitive(self, user):
        ProjectFactory(user=user, name="My API")
        with pytest.raises(IntegrityError):
            with transaction.atomic():
                ProjectFactory(user=user, name="my api")

    def test_same_name_allowed_across_users(self, user, other_user):
        ProjectFactory(user=user, name="Shared")
        # Different owner → no clash.
        ProjectFactory(user=other_user, name="Shared")
        assert Project.objects.filter(name="Shared").count() == 2

    def test_for_user_scopes_to_owner(self, user, other_user):
        mine = ProjectFactory(user=user)
        ProjectFactory(user=other_user)
        assert list(Project.objects.for_user(user)) == [mine]

    def test_with_session_stats_annotations(self, user):
        project = ProjectFactory(user=user)
        DebugSessionFactory.create_batch(2, project=project)
        annotated = Project.objects.for_user(user).with_session_stats().get(pk=project.pk)
        assert annotated.session_count == 2
        assert annotated.latest_session_at is not None


class TestDebugSessionConstraints:
    def test_rejects_unknown_status(self):
        with pytest.raises(IntegrityError):
            with transaction.atomic():
                DebugSessionFactory(status="exploded")

    def test_default_status_is_pending(self):
        assert DebugSessionFactory().status == SessionStatus.PENDING

    def test_owner_id_resolves_through_project(self, user):
        session = DebugSessionFactory(project__user=user)
        assert session.owner_id == user.id


class TestUploadedFileConstraints:
    def test_duplicate_content_in_session_rejected(self):
        session = DebugSessionFactory()
        digest = "a" * 64
        UploadedFileFactory(debug_session=session, content_sha256=digest)
        with pytest.raises(IntegrityError):
            with transaction.atomic():
                UploadedFileFactory(debug_session=session, content_sha256=digest)

    def test_same_content_allowed_in_different_sessions(self):
        digest = "b" * 64
        UploadedFileFactory(content_sha256=digest)
        # Different session → constraint does not apply.
        UploadedFileFactory(content_sha256=digest)


class TestDetectedIssue:
    @pytest.mark.parametrize("bad_hint", [1.0, 1.4, -0.01])
    def test_confidence_hint_must_be_in_range(self, bad_hint):
        with pytest.raises(IntegrityError):
            with transaction.atomic():
                DetectedIssueFactory(confidence_hint=bad_hint)

    def test_by_priority_orders_worst_first(self):
        session = DebugSessionFactory()
        low = DetectedIssueFactory(
            debug_session=session, severity=Severity.LOW, confidence_hint=0.9
        )
        high = DetectedIssueFactory(
            debug_session=session, severity=Severity.HIGH, confidence_hint=0.1
        )
        medium = DetectedIssueFactory(
            debug_session=session, severity=Severity.MEDIUM, confidence_hint=0.5
        )
        ordered = list(DetectedIssue.objects.filter(debug_session=session).by_priority())
        assert ordered == [high, medium, low]


class TestDiagnosisReport:
    @pytest.mark.parametrize("bad_score", [1.0, 2.0, -0.5])
    def test_confidence_score_must_be_in_range(self, bad_score):
        with pytest.raises(IntegrityError):
            with transaction.atomic():
                DiagnosisReportFactory(confidence_score=bad_score)

    def test_rejects_unknown_severity(self):
        with pytest.raises(IntegrityError):
            with transaction.atomic():
                DiagnosisReportFactory(severity="catastrophic")

    def test_one_report_per_session(self):
        session = DebugSessionFactory()
        DiagnosisReportFactory(debug_session=session)
        with pytest.raises(IntegrityError):
            with transaction.atomic():
                DiagnosisReportFactory(debug_session=session)

    def test_owner_id_resolves_through_chain(self, user):
        report = DiagnosisReportFactory(debug_session__project__user=user)
        assert report.owner_id == user.id
