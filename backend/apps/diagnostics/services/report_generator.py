"""
Report generation orchestrator — the analyze pipeline (docs/AGENT_PLAN.md §12).

    1. mark session analyzing
    2. run the rule detector -> persist DetectedIssue rows
    3. build the evidence bundle (redacted, budget-capped)
    4. call the AI -> parse -> validate (report_schema) -> check evidence sources
    5. apply confidence / missing-information guardrails
    6. persist the DiagnosisReport and mark the session completed

On parse/validation/transport failure the call is retried once (configurable via
``PATCHPATH_AI["MAX_RETRIES"]``), feeding the prior errors back to the model. If
every attempt fails the session is marked ``failed`` with a reason, while its
uploaded files and detected issues are preserved for the user to inspect.
"""

from __future__ import annotations

import json
import logging

from django.conf import settings
from django.db import transaction
from django.utils import timezone
from pydantic import ValidationError

from ..models import DebugSession, DiagnosisReport, SessionStatus
from . import evidence_builder, rule_detector
from .ai_client import AIClient, AIClientError, AIResult
from .report_schema import DiagnosisReportModel

logger = logging.getLogger("patchpath.diagnostics")

# Confidence ceiling when the deterministic detector found nothing to stand on.
_NO_ISSUES_CONFIDENCE_CAP = 0.35

SYSTEM_PROMPT = (
    "You are PatchPath, a careful deployment diagnostics assistant. Diagnose "
    "production deployment failures using only the provided evidence extracted "
    "from uploaded files and logs. Do not invent files, log lines, commands, "
    "platforms, or configuration values. Never claim certainty. Use careful "
    'language such as "most likely", "based on uploaded evidence", and '
    '"possible fix". Always include confidence, evidence, missing information, '
    "possible risks, and verification steps. Never auto-apply fixes."
)

# Compact, example-driven contract — clearer to the model than a raw JSON Schema.
_SCHEMA_HINT = """\
Return ONLY a JSON object with exactly these keys:
{
  "root_cause": string,
  "confidence_score": number >= 0 and < 1 (never 1.0),
  "severity": "low" | "medium" | "high",
  "detected_stack": string[],
  "detected_cloud_provider": string,
  "explanation": string,
  "evidence": [{"source": string, "line_or_section": string, "reason": string}],
  "recommended_fix": string,
  "commands_to_run": string[],
  "verification_checklist": string[],
  "missing_information": string[],
  "possible_risks": string[]
}
Every "source" in evidence MUST be one of the uploaded filenames. Always include
at least one item in "missing_information"."""


class AnalysisError(Exception):
    """Raised when analysis cannot produce a valid report after all retries."""


def _build_user_prompt(bundle: dict, retry_note: str = "") -> str:
    parts = [
        "Return only valid JSON matching the schema.",
        "",
        "Task: identify the most likely deployment failure root cause from the "
        "evidence bundle. Base every conclusion on the provided evidence; lower "
        "confidence when evidence is weak; never claim 100% certainty.",
        "",
        "Project metadata:",
        json.dumps(bundle["project_metadata"], ensure_ascii=False),
        "",
        "Detected rule matches:",
        json.dumps(bundle["detected_issues"], ensure_ascii=False),
        "",
        "Evidence bundle:",
        json.dumps(bundle, ensure_ascii=False),
        "",
        _SCHEMA_HINT,
    ]
    if retry_note:
        parts += [
            "",
            "Your previous response was rejected for the following reason. "
            "Return corrected JSON only:",
            retry_note,
        ]
    return "\n".join(parts)


def _issue_payloads(detected) -> list[dict]:
    return [
        {
            "issue_type": d.issue_type,
            "severity": d.severity,
            "confidence_hint": d.confidence_hint,
            "matched_pattern": d.matched_pattern,
            "evidence": d.evidence,
        }
        for d in detected
    ]


def _session_files(session: DebugSession) -> list[dict]:
    return [
        {
            "filename": f.filename,
            "file_type": f.file_type,
            "content": f.content,
            "line_count": f.line_count,
        }
        for f in session.files.all()
    ]


def _validate_evidence_sources(model: DiagnosisReportModel, known: set[str]) -> None:
    """Reject reports that cite files the user never uploaded (§12)."""
    if not known:
        return
    for item in model.evidence:
        if item.source not in known:
            raise ValueError(
                f"evidence cites unknown source '{item.source}'; allowed sources: "
                f"{sorted(known)}"
            )


def _apply_guardrails(
    model: DiagnosisReportModel, has_detected_issues: bool
) -> DiagnosisReportModel:
    """Cap confidence by evidence quality and guarantee a missing-info item."""
    ceiling = settings.PATCHPATH_MAX_CONFIDENCE
    if not has_detected_issues:
        ceiling = min(ceiling, _NO_ISSUES_CONFIDENCE_CAP)
    model.confidence_score = round(min(model.confidence_score, ceiling), 4)

    if not model.missing_information:
        model.missing_information = [
            "The model did not flag any gaps; treat this diagnosis as provisional "
            "and verify against the live deployment."
        ]
    return model


def _persist_report(
    session: DebugSession, model: DiagnosisReportModel, ai: AIResult
) -> DiagnosisReport:
    fields = model.to_orm_fields()
    fields.update(
        model_name=ai.model,
        prompt_tokens=ai.prompt_tokens,
        completion_tokens=ai.completion_tokens,
    )
    with transaction.atomic():
        report, _ = DiagnosisReport.objects.update_or_create(debug_session=session, defaults=fields)
        session.status = SessionStatus.COMPLETED
        session.failure_reason = ""
        session.analysis_completed_at = timezone.now()
        session.save(
            update_fields=["status", "failure_reason", "analysis_completed_at", "updated_at"]
        )
    return report


def _mark_failed(session: DebugSession, reason: str) -> None:
    session.status = SessionStatus.FAILED
    session.failure_reason = reason[:500]
    session.analysis_completed_at = timezone.now()
    session.save(update_fields=["status", "failure_reason", "analysis_completed_at", "updated_at"])


def run_analysis(session: DebugSession, *, client: AIClient | None = None) -> DiagnosisReport:
    """Run the full analyze pipeline for ``session`` and return its report.

    Raises :class:`AnalysisError` if no valid report could be produced; the
    session is left ``failed`` (with detected issues + files intact) in that case.
    """
    client = client or AIClient()

    session.status = SessionStatus.ANALYZING
    session.analysis_started_at = timezone.now()
    session.failure_reason = ""
    session.save(update_fields=["status", "analysis_started_at", "failure_reason", "updated_at"])

    # Deterministic layer first — persisted regardless of what the model does.
    detected = rule_detector.run_for_session(session)
    issue_payloads = _issue_payloads(detected)
    files = _session_files(session)

    bundle = evidence_builder.build_bundle(
        {"stack": session.project.stack, "cloud_provider": session.project.cloud_provider},
        files,
        issue_payloads,
        char_budget=settings.PATCHPATH_EVIDENCE_CHAR_BUDGET,
    )
    known = evidence_builder.known_sources(files)

    attempts = settings.PATCHPATH_AI.get("MAX_RETRIES", 1) + 1
    retry_note = ""
    last_error = "AI analysis failed."

    for attempt in range(1, attempts + 1):
        prompt = _build_user_prompt(bundle, retry_note)
        try:
            ai = client.generate(SYSTEM_PROMPT, prompt)
            data = json.loads(ai.text)
            model = DiagnosisReportModel.model_validate(data)
            _validate_evidence_sources(model, known)
        except AIClientError as exc:
            last_error = f"AI service error: {exc}"
            retry_note = "The previous attempt failed to return a response."
            logger.warning("Analysis attempt %s/%s failed: %s", attempt, attempts, last_error)
            continue
        except (json.JSONDecodeError, ValidationError, ValueError) as exc:
            last_error = f"Invalid AI response: {exc}"
            retry_note = str(exc)
            logger.warning("Analysis attempt %s/%s rejected: %s", attempt, attempts, exc)
            continue

        model = _apply_guardrails(model, has_detected_issues=bool(issue_payloads))
        return _persist_report(session, model, ai)

    _mark_failed(session, last_error)
    logger.warning("Analysis failed for session %s after %s attempts", session.pk, attempts)
    raise AnalysisError(last_error)
