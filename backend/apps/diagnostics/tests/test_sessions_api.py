"""API tests for the diagnostics domain: projects, sessions, reports, dashboard.

Focus areas (docs/AGENT_PLAN.md §8, §9, §17):
    - auth is required everywhere
    - every read/write is scoped to the owner (foreign objects → 404)
    - response payloads carry the contract fields the frontend depends on
"""

from __future__ import annotations

import pytest
from django.urls import reverse
from rest_framework import status

from apps.diagnostics.models import DebugSession, Project, SessionStatus, Severity

from .factories import (
    DebugSessionFactory,
    DetectedIssueFactory,
    DiagnosisReportFactory,
    ProjectFactory,
    UploadedFileFactory,
)

pytestmark = pytest.mark.django_db


# URL helpers --------------------------------------------------------------

DASHBOARD = reverse("api:diagnostics:dashboard")
PROJECT_LIST = reverse("api:diagnostics:project-list")


def project_detail_url(project_id):
    return reverse("api:diagnostics:project-detail", kwargs={"project_id": project_id})


def project_sessions_url(project_id):
    return reverse("api:diagnostics:project-session-list", kwargs={"project_id": project_id})


def session_detail_url(session_id):
    return reverse("api:diagnostics:session-detail", kwargs={"session_id": session_id})


def report_detail_url(report_id):
    return reverse("api:diagnostics:report-detail", kwargs={"report_id": report_id})


# Auth ---------------------------------------------------------------------


class TestAuthRequired:
    @pytest.mark.parametrize("url", [DASHBOARD, PROJECT_LIST])
    def test_anonymous_is_rejected(self, api_client, url):
        assert api_client.get(url).status_code == status.HTTP_401_UNAUTHORIZED


# Projects -----------------------------------------------------------------


class TestProjects:
    def test_create_project(self, auth_client, user):
        resp = auth_client.post(
            PROJECT_LIST,
            {"name": "Django Render API", "stack": "Django", "cloud_provider": "Render"},
            format="json",
        )
        assert resp.status_code == status.HTTP_201_CREATED
        assert resp.data["name"] == "Django Render API"
        assert resp.data["session_count"] == 0
        assert Project.objects.filter(user=user, name="Django Render API").exists()

    def test_list_is_scoped_to_owner(self, auth_client, user, other_user):
        ProjectFactory(user=user, name="Mine")
        ProjectFactory(user=other_user, name="Theirs")
        resp = auth_client.get(PROJECT_LIST)
        assert resp.status_code == status.HTTP_200_OK
        assert [p["name"] for p in resp.data["results"]] == ["Mine"]

    def test_list_includes_session_count(self, auth_client, user):
        project = ProjectFactory(user=user)
        DebugSessionFactory.create_batch(3, project=project)
        resp = auth_client.get(PROJECT_LIST)
        assert resp.data["results"][0]["session_count"] == 3

    def test_duplicate_name_rejected_case_insensitive(self, auth_client, user):
        ProjectFactory(user=user, name="Acme")
        resp = auth_client.post(PROJECT_LIST, {"name": "acme"}, format="json")
        assert resp.status_code == status.HTTP_400_BAD_REQUEST
        assert "name" in resp.data

    def test_detail_returns_recent_sessions(self, auth_client, user):
        project = ProjectFactory(user=user)
        DebugSessionFactory(project=project)
        resp = auth_client.get(project_detail_url(project.id))
        assert resp.status_code == status.HTTP_200_OK
        assert len(resp.data["recent_sessions"]) == 1

    def test_detail_foreign_project_returns_404(self, auth_client, other_user):
        project = ProjectFactory(user=other_user)
        assert (
            auth_client.get(project_detail_url(project.id)).status_code == status.HTTP_404_NOT_FOUND
        )


# Sessions -----------------------------------------------------------------


class TestSessions:
    def test_create_session_under_own_project(self, auth_client, user):
        project = ProjectFactory(user=user)
        resp = auth_client.post(
            project_sessions_url(project.id),
            {"error_summary": "Deploy fails on boot"},
            format="json",
        )
        assert resp.status_code == status.HTTP_201_CREATED
        assert resp.data["status"] == SessionStatus.PENDING
        assert resp.data["project_id"] == str(project.id)
        assert resp.data["report_id"] is None

    def test_cannot_create_session_under_foreign_project(self, auth_client, other_user):
        project = ProjectFactory(user=other_user)
        resp = auth_client.post(
            project_sessions_url(project.id),
            {"error_summary": "nope"},
            format="json",
        )
        assert resp.status_code == status.HTTP_404_NOT_FOUND
        assert not DebugSession.objects.filter(project=project).exists()

    def test_list_sessions_scoped_to_project(self, auth_client, user):
        project = ProjectFactory(user=user)
        DebugSessionFactory.create_batch(2, project=project)
        # Noise under a different project of the same user.
        DebugSessionFactory(project=ProjectFactory(user=user))
        resp = auth_client.get(project_sessions_url(project.id))
        assert resp.status_code == status.HTTP_200_OK
        assert resp.data["count"] == 2

    def test_detail_includes_files_issues_and_report(self, auth_client, user):
        session = DebugSessionFactory(project__user=user)
        UploadedFileFactory(debug_session=session)
        DetectedIssueFactory(debug_session=session)
        DiagnosisReportFactory(debug_session=session, severity=Severity.HIGH)
        resp = auth_client.get(session_detail_url(session.id))
        assert resp.status_code == status.HTTP_200_OK
        assert len(resp.data["files"]) == 1
        assert len(resp.data["detected_issues"]) == 1
        assert resp.data["report"]["severity"] == Severity.HIGH
        # Metadata only — content must never be serialized.
        assert "content" not in resp.data["files"][0]

    def test_detail_without_report_is_null(self, auth_client, user):
        session = DebugSessionFactory(project__user=user)
        resp = auth_client.get(session_detail_url(session.id))
        assert resp.data["report"] is None

    def test_foreign_session_returns_404(self, auth_client, other_user):
        session = DebugSessionFactory(project__user=other_user)
        assert (
            auth_client.get(session_detail_url(session.id)).status_code == status.HTTP_404_NOT_FOUND
        )


# Reports ------------------------------------------------------------------


class TestReports:
    def test_full_report_document(self, auth_client, user):
        report = DiagnosisReportFactory(debug_session__project__user=user)
        resp = auth_client.get(report_detail_url(report.id))
        assert resp.status_code == status.HTTP_200_OK
        assert resp.data["recommended_fix"]
        assert "evidence_json" in resp.data
        assert "verification_checklist_json" in resp.data
        assert resp.data["confidence_score"] < 1.0

    def test_foreign_report_returns_404(self, auth_client, other_user):
        report = DiagnosisReportFactory(debug_session__project__user=other_user)
        assert (
            auth_client.get(report_detail_url(report.id)).status_code == status.HTTP_404_NOT_FOUND
        )


# Dashboard ----------------------------------------------------------------


class TestDashboard:
    def test_counts_are_scoped_to_user(self, auth_client, user, other_user):
        project = ProjectFactory(user=user)
        DebugSessionFactory(project=project, status=SessionStatus.COMPLETED)
        DebugSessionFactory(project=project, status=SessionStatus.FAILED)
        high_session = DebugSessionFactory(project=project, status=SessionStatus.COMPLETED)
        DiagnosisReportFactory(debug_session=high_session, severity=Severity.HIGH)

        # Another user's data must not leak into the totals.
        ProjectFactory(user=other_user)
        DiagnosisReportFactory(debug_session__project__user=other_user, severity=Severity.HIGH)

        resp = auth_client.get(DASHBOARD)
        assert resp.status_code == status.HTTP_200_OK
        assert resp.data["project_count"] == 1
        assert resp.data["session_count"] == 3
        assert resp.data["completed_session_count"] == 2
        assert resp.data["failed_session_count"] == 1
        assert resp.data["high_severity_count"] == 1
        assert len(resp.data["recent_sessions"]) == 3
        assert len(resp.data["recent_reports"]) == 1
