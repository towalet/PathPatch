"""
Rule detector tests (docs/AGENT_PLAN.md §10 + §17).

Coverage:
    - one fixture per MVP rule triggers its expected issue type
    - a clean log fabricates nothing
    - evidence is de-duplicated and capped at 5 items
    - provider-specific rules stay gated to their context
    - secrets redacted upstream never reach evidence snippets
    - issues are ranked worst-first
    - run_for_session persists issues and is idempotent
"""

from __future__ import annotations

from pathlib import Path

import pytest

from apps.diagnostics.models import DetectedIssue, Severity
from apps.diagnostics.services import redaction, rule_detector
from apps.diagnostics.services.rule_definitions import RULES
from apps.diagnostics.services.rule_detector import detect

from .factories import DebugSessionFactory, UploadedFileFactory

FIXTURES = Path(__file__).parent / "fixtures"

# A project with no provider/stack hints, so context-gated rules only fire when
# the *content* itself names the provider.
NEUTRAL = {"stack": "", "cloud_provider": ""}


def load(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def one_file(content: str, *, name: str = "deploy.log", file_type: str = "log") -> list[dict]:
    return [
        {
            "filename": name,
            "file_type": file_type,
            "content": content,
            "line_count": content.count("\n") + 1,
        }
    ]


def types_of(issues: list[dict]) -> set[str]:
    return {issue["issue_type"] for issue in issues}


# ---------------------------------------------------------------------------
# One fixture per MVP rule
# ---------------------------------------------------------------------------

# (fixture filename, expected issue_type). Every rule in RULES must appear here.
FIXTURE_CASES = [
    ("django_missing_database_url.log", "missing_database_url"),
    ("missing_env_var.log", "missing_env_var"),
    ("port_binding_issue.log", "port_binding_issue"),
    ("python_missing_dependency.log", "missing_python_dependency"),
    ("node_missing_dependency.log", "missing_node_dependency"),
    ("npm_build_failure.log", "npm_build_failure"),
    ("cors_error.log", "cors_error"),
    ("django_staticfiles_issue.log", "django_staticfiles_issue"),
    ("postgres_connection_refused.log", "postgres_connection_refused"),
    ("docker_build_failed.log", "docker_build_failed"),
    ("render_wrong_start_command.log", "wrong_start_command"),
    ("collectstatic_failed.log", "collectstatic_failed"),
    ("vercel_build_command_issue.log", "vercel_build_command_issue"),
    ("render_railway_env_mismatch.log", "render_railway_env_mismatch"),
]


@pytest.mark.parametrize(("fixture", "expected"), FIXTURE_CASES)
def test_fixture_triggers_expected_issue(fixture, expected):
    issues = detect(NEUTRAL, one_file(load(fixture), name=fixture))
    assert expected in types_of(issues)


def test_every_rule_has_a_fixture_case():
    """Guard: the parametrized table must cover the whole registry."""
    covered = {expected for _, expected in FIXTURE_CASES}
    assert covered == {rule.issue_type for rule in RULES}


def test_detected_issue_payload_shape():
    [issue, *_] = detect(NEUTRAL, one_file(load("django_missing_database_url.log")))
    assert set(issue) == {
        "issue_type",
        "severity",
        "confidence_hint",
        "matched_pattern",
        "evidence",
    }
    assert 0 <= issue["confidence_hint"] < 1
    assert issue["evidence"], "a detected issue must carry evidence"
    [evidence, *_] = issue["evidence"]
    assert set(evidence) == {"source", "line_or_section", "snippet", "reason"}


# ---------------------------------------------------------------------------
# No fabrication
# ---------------------------------------------------------------------------


def test_clean_log_produces_no_issues():
    issues = detect(NEUTRAL, one_file(load("clean_healthy_deploy.log")))
    assert issues == []


def test_empty_input_produces_no_issues():
    assert detect(NEUTRAL, []) == []
    assert detect(NEUTRAL, one_file("")) == []


# ---------------------------------------------------------------------------
# Evidence shaping: cap + de-duplication
# ---------------------------------------------------------------------------


def test_evidence_is_capped_at_five_items():
    body = "\n".join(f"ModuleNotFoundError: No module named 'pkg{i}'" for i in range(8))
    [issue] = [
        i for i in detect(NEUTRAL, one_file(body)) if i["issue_type"] == "missing_python_dependency"
    ]
    assert len(issue["evidence"]) == 5
    # Many matches nudge confidence up but never to certainty.
    assert issue["confidence_hint"] < 1


def test_near_identical_evidence_is_deduplicated():
    # Same matching line in two files -> a single evidence item, not two.
    files = one_file("Cannot find module 'express'", name="a.log") + one_file(
        "Cannot find module 'express'", name="b.log"
    )
    [issue] = [i for i in detect(NEUTRAL, files) if i["issue_type"] == "missing_node_dependency"]
    assert len(issue["evidence"]) == 1


# ---------------------------------------------------------------------------
# Provider gating
# ---------------------------------------------------------------------------


def test_provider_rule_is_gated_by_context():
    body = "RuntimeError: environment variable STRIPE_KEY is not set"
    # No Render/Railway signal anywhere -> the provider rule stays silent...
    assert "render_railway_env_mismatch" not in types_of(detect(NEUTRAL, one_file(body)))
    # ...but project metadata is enough to satisfy the gate.
    with_provider = detect({"stack": "", "cloud_provider": "Railway"}, one_file(body))
    assert "render_railway_env_mismatch" in types_of(with_provider)


def test_vercel_rule_does_not_trip_on_unrelated_log():
    issues = detect(NEUTRAL, one_file(load("django_missing_database_url.log")))
    assert "vercel_build_command_issue" not in types_of(issues)


# ---------------------------------------------------------------------------
# Secret safety + ranking
# ---------------------------------------------------------------------------


def test_secrets_never_appear_in_evidence():
    raw = "DATABASE_URL=postgres://admin:supersecretpw@db:5432/app\n" "KeyError: 'DATABASE_URL'\n"
    redacted = redaction.redact(raw).text
    issues = detect(NEUTRAL, one_file(redacted))
    assert "missing_database_url" in types_of(issues)
    blob = "".join(e["snippet"] for issue in issues for e in issue["evidence"])
    assert "supersecretpw" not in blob


def test_issues_are_ranked_worst_first():
    files = one_file(load("cors_error.log"), name="cors.log") + one_file(
        load("postgres_connection_refused.log"), name="db.log"
    )
    issues = detect(NEUTRAL, files)
    severities = [issue["severity"] for issue in issues]
    # high outranks medium; the first issue is always the worst.
    assert severities[0] == Severity.HIGH
    assert severities == sorted(
        severities, key=lambda s: {Severity.HIGH: 0, Severity.MEDIUM: 1, Severity.LOW: 2}[s]
    )


# ---------------------------------------------------------------------------
# Persistence (DB)
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestRunForSession:
    def _session_with(self, content: str, *, provider: str = "Render"):
        session = DebugSessionFactory(project__cloud_provider=provider)
        UploadedFileFactory(
            debug_session=session,
            filename="deploy.log",
            file_type="log",
            content=content,
        )
        return session

    def test_persists_detected_issues(self):
        session = self._session_with(load("django_missing_database_url.log"))
        created = rule_detector.run_for_session(session)

        assert created
        stored = DetectedIssue.objects.filter(debug_session=session)
        assert stored.filter(issue_type="missing_database_url").exists()
        issue = stored.get(issue_type="missing_database_url")
        assert issue.evidence and isinstance(issue.evidence, list)
        assert 0 <= issue.confidence_hint < 1

    def test_is_idempotent(self):
        session = self._session_with(load("django_missing_database_url.log"))
        rule_detector.run_for_session(session)
        first = DetectedIssue.objects.filter(debug_session=session).count()

        rule_detector.run_for_session(session)
        second = DetectedIssue.objects.filter(debug_session=session).count()

        assert first == second
        assert (
            DetectedIssue.objects.filter(
                debug_session=session, issue_type="missing_database_url"
            ).count()
            == 1
        )

    def test_clean_log_persists_nothing(self):
        session = self._session_with(load("clean_healthy_deploy.log"), provider="")
        created = rule_detector.run_for_session(session)
        assert created == []
        assert not DetectedIssue.objects.filter(debug_session=session).exists()
