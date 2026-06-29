"""
Report generator + analyze endpoint tests with a mocked AI client
(docs/AGENT_PLAN.md §12 + §17).

The AI seam is always faked — the suite never touches the network. Coverage:
schema validation, retry-on-bad-output, transport-error retry, confidence and
missing-information guardrails, the failed-session path, and the analyze view.
"""

from __future__ import annotations

import json

import pytest
from django.urls import reverse
from rest_framework import status

from apps.diagnostics.models import (
    DetectedIssue,
    DiagnosisReport,
    SessionStatus,
    UploadedFile,
)
from apps.diagnostics.services import report_generator
from apps.diagnostics.services.ai_client import AIClientError, AIResult
from apps.diagnostics.services.report_schema import (
    LOW_CONFIDENCE_CEILING,
    DiagnosisReportModel,
)

from .factories import DebugSessionFactory, UploadedFileFactory

# A log the deterministic detector will match (missing_database_url etc.).
ERROR_LOG = "Booting...\nKeyError: 'DATABASE_URL'\ndjango...ImproperlyConfigured\n"
CLEAN_LOG = "Build successful\nListening at http://0.0.0.0:10000\nService is live\n"


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


class FakeAIClient:
    """Replays a queued list of AIResult / Exception items, counting calls."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = 0

    def generate(self, system_prompt: str, user_prompt: str) -> AIResult:
        self.calls += 1
        item = self._responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def payload(**overrides) -> dict:
    base = {
        "root_cause": "DATABASE_URL was most likely unset at startup.",
        "confidence_score": 0.8,
        "severity": "high",
        "detected_stack": ["Django"],
        "detected_cloud_provider": "Render",
        "explanation": "The log shows a KeyError for DATABASE_URL during boot.",
        "evidence": [
            {
                "source": "deploy.log",
                "line_or_section": "line 2",
                "reason": "Log shows KeyError for DATABASE_URL.",
            }
        ],
        "recommended_fix": "Set DATABASE_URL in the service environment and redeploy.",
        "commands_to_run": ["render env:set DATABASE_URL=..."],
        "verification_checklist": ["Confirm the service boots without a KeyError."],
        "missing_information": ["settings.py was not uploaded."],
        "possible_risks": [],
    }
    base.update(overrides)
    return base


def ai_result(body, model: str = "test-model") -> AIResult:
    text = body if isinstance(body, str) else json.dumps(body)
    return AIResult(text=text, model=model, prompt_tokens=11, completion_tokens=22)


def make_session(content: str = ERROR_LOG, *, user=None, provider: str = "Render"):
    kwargs = {"project__cloud_provider": provider}
    if user is not None:
        kwargs["project__user"] = user
    session = DebugSessionFactory(**kwargs)
    UploadedFileFactory(
        debug_session=session, filename="deploy.log", file_type="log", content=content
    )
    return session


# ---------------------------------------------------------------------------
# Schema (pure, no DB)
# ---------------------------------------------------------------------------


class TestReportSchema:
    def test_valid_payload_parses(self):
        model = DiagnosisReportModel.model_validate(payload())
        assert model.confidence_score == 0.8
        assert model.evidence[0].source == "deploy.log"

    def test_rejects_confidence_above_one(self):
        with pytest.raises(ValueError):
            DiagnosisReportModel.model_validate(payload(confidence_score=1.5))

    def test_rejects_unknown_severity(self):
        with pytest.raises(ValueError):
            DiagnosisReportModel.model_validate(payload(severity="catastrophic"))

    def test_requires_evidence_when_confident(self):
        with pytest.raises(ValueError):
            DiagnosisReportModel.model_validate(payload(confidence_score=0.8, evidence=[]))

    def test_allows_empty_evidence_when_low_confidence(self):
        model = DiagnosisReportModel.model_validate(
            payload(confidence_score=LOW_CONFIDENCE_CEILING - 0.1, evidence=[])
        )
        assert model.evidence == []

    def test_to_orm_fields_renames_keys(self):
        fields = DiagnosisReportModel.model_validate(payload()).to_orm_fields()
        assert fields["commands_json"] == ["render env:set DATABASE_URL=..."]
        assert "evidence_json" in fields and "commands_to_run" not in fields


# ---------------------------------------------------------------------------
# Generator pipeline (DB)
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestRunAnalysis:
    def test_happy_path_persists_report_and_issues(self):
        session = make_session()
        client = FakeAIClient([ai_result(payload())])

        report = report_generator.run_analysis(session, client=client)

        session.refresh_from_db()
        assert session.status == SessionStatus.COMPLETED
        assert session.analysis_started_at and session.analysis_completed_at
        assert report.confidence_score < 1
        assert report.evidence_json and report.commands_json
        assert report.model_name == "test-model"
        assert report.prompt_tokens == 11
        # Deterministic layer ran and persisted.
        assert DetectedIssue.objects.filter(debug_session=session).exists()

    def test_retries_once_on_invalid_json(self):
        session = make_session()
        client = FakeAIClient([ai_result("not-json{"), ai_result(payload())])

        report = report_generator.run_analysis(session, client=client)

        assert client.calls == 2
        assert report.debug_session_id == session.id

    def test_retries_on_schema_validation_error(self):
        session = make_session()
        client = FakeAIClient([ai_result(payload(confidence_score=2.0)), ai_result(payload())])

        report_generator.run_analysis(session, client=client)
        assert client.calls == 2

    def test_retries_on_unknown_evidence_source(self):
        session = make_session()
        ghost = payload(
            evidence=[{"source": "ghost.log", "line_or_section": "line 9", "reason": "made up"}]
        )
        client = FakeAIClient([ai_result(ghost), ai_result(payload())])

        report = report_generator.run_analysis(session, client=client)
        assert client.calls == 2
        assert all(e["source"] == "deploy.log" for e in report.evidence_json)

    def test_transport_error_is_retried_then_succeeds(self):
        session = make_session()
        client = FakeAIClient([AIClientError("connection reset"), ai_result(payload())])

        report = report_generator.run_analysis(session, client=client)
        assert client.calls == 2
        assert report.id

    def test_exhausted_retries_marks_session_failed(self):
        session = make_session()
        client = FakeAIClient([ai_result("bad-1{"), ai_result("bad-2{")])

        with pytest.raises(report_generator.AnalysisError):
            report_generator.run_analysis(session, client=client)

        session.refresh_from_db()
        assert session.status == SessionStatus.FAILED
        assert session.failure_reason
        # Files + detected issues are preserved for inspection.
        assert UploadedFile.objects.filter(debug_session=session).exists()
        assert DetectedIssue.objects.filter(debug_session=session).exists()
        assert not DiagnosisReport.objects.filter(debug_session=session).exists()

    def test_confidence_is_clamped_below_one(self):
        session = make_session()
        client = FakeAIClient([ai_result(payload(confidence_score=1.0))])

        report = report_generator.run_analysis(session, client=client)
        assert report.confidence_score < 1
        assert report.confidence_score == pytest.approx(0.99)

    def test_no_detected_issues_caps_confidence(self):
        # A clean log yields no rule matches -> confidence capped hard.
        session = make_session(CLEAN_LOG, provider="")
        client = FakeAIClient([ai_result(payload(confidence_score=0.9))])

        report = report_generator.run_analysis(session, client=client)
        assert not DetectedIssue.objects.filter(debug_session=session).exists()
        assert report.confidence_score <= 0.35

    def test_missing_information_is_always_present(self):
        session = make_session()
        client = FakeAIClient([ai_result(payload(missing_information=[]))])

        report = report_generator.run_analysis(session, client=client)
        assert len(report.missing_information_json) >= 1

    def test_reanalyze_is_idempotent(self):
        session = make_session()
        report_generator.run_analysis(session, client=FakeAIClient([ai_result(payload())]))
        report_generator.run_analysis(session, client=FakeAIClient([ai_result(payload())]))

        assert DiagnosisReport.objects.filter(debug_session=session).count() == 1


# ---------------------------------------------------------------------------
# Analyze endpoint (integration)
# ---------------------------------------------------------------------------


def analyze_url(session_id):
    return reverse("api:diagnostics:session-analyze", kwargs={"session_id": session_id})


@pytest.mark.django_db
class TestAnalyzeEndpoint:
    def _patch_ai(self, monkeypatch, responses):
        fake = FakeAIClient(responses)
        monkeypatch.setattr(report_generator, "AIClient", lambda *a, **k: fake)
        return fake

    def test_success_returns_completed_with_report_id(self, auth_client, user, monkeypatch):
        self._patch_ai(monkeypatch, [ai_result(payload())])
        session = make_session(user=user)

        resp = auth_client.post(analyze_url(session.id))

        assert resp.status_code == status.HTTP_200_OK
        assert resp.data["status"] == SessionStatus.COMPLETED
        assert resp.data["report_id"]

    def test_requires_evidence(self, auth_client, user):
        session = DebugSessionFactory(project__user=user)
        resp = auth_client.post(analyze_url(session.id))
        assert resp.status_code == status.HTTP_400_BAD_REQUEST

    def test_requires_authentication(self, api_client, user):
        session = make_session(user=user)
        resp = api_client.post(analyze_url(session.id))
        assert resp.status_code == status.HTTP_401_UNAUTHORIZED

    def test_cannot_analyze_foreign_session(self, auth_client, other_user):
        session = make_session(user=other_user)
        resp = auth_client.post(analyze_url(session.id))
        assert resp.status_code == status.HTTP_404_NOT_FOUND

    def test_ai_failure_returns_502_and_failed_status(self, auth_client, user, monkeypatch):
        self._patch_ai(monkeypatch, [ai_result("bad{"), ai_result("bad{")])
        session = make_session(user=user)

        resp = auth_client.post(analyze_url(session.id))

        assert resp.status_code == status.HTTP_502_BAD_GATEWAY
        assert resp.data["status"] == SessionStatus.FAILED
        assert resp.data["report_id"] is None
        session.refresh_from_db()
        assert session.status == SessionStatus.FAILED
