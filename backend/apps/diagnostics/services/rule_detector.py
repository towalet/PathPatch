"""
Rule-based issue detector (docs/AGENT_PLAN.md §10).

Consumes redacted file content + project metadata and produces ``DetectedIssue``
payloads with evidence snippets. Two entry points:

* :func:`detect` — pure, side-effect-free. Takes plain dicts, returns ranked
  issue dicts. This is the unit-tested core.
* :func:`run_for_session` — adapts a ``DebugSession`` to :func:`detect` and
  persists the result as ``DetectedIssue`` rows (idempotent: a re-run replaces
  the previous detection for that session).

Evidence discipline (per §10):
    - snippet = matched line ± up to 2 lines of context, capped at 500 chars
    - at most 5 evidence items per issue, near-identical snippets de-duplicated
    - issues ranked worst-first: severity, then confidence_hint
    - no match -> no issue (the detector never fabricates)
"""

from __future__ import annotations

from dataclasses import dataclass

from ..models import DebugSession, DetectedIssue, Severity
from .rule_definitions import RULES, Rule

# Evidence shaping limits (§10).
_CONTEXT_LINES = 2
_SNIPPET_MAX_CHARS = 500
_MAX_EVIDENCE_PER_ISSUE = 5
_MATCHED_PATTERN_MAX = 255
# Confidence hints stay strictly below 1.0 (DetectedIssue DB constraint).
_MAX_CONFIDENCE_HINT = 0.95

_SEVERITY_RANK = {Severity.HIGH: 0, Severity.MEDIUM: 1, Severity.LOW: 2}


@dataclass
class _Evidence:
    source: str
    line_or_section: str
    snippet: str
    reason: str

    def as_dict(self) -> dict:
        return {
            "source": self.source,
            "line_or_section": self.line_or_section,
            "snippet": self.snippet,
            "reason": self.reason,
        }


def _snippet_for(lines: list[str], index: int) -> str:
    """The matched line plus up to ``_CONTEXT_LINES`` of surrounding context."""
    start = max(0, index - _CONTEXT_LINES)
    end = min(len(lines), index + _CONTEXT_LINES + 1)
    block = "\n".join(lines[start:end]).strip()
    return block[:_SNIPPET_MAX_CHARS]


def _context_satisfied(rule: Rule, haystack: str) -> bool:
    """Provider gate: a context-bound rule only fires when a token is present."""
    if not rule.context:
        return True
    return any(token in haystack for token in rule.context)


def _match_rule(rule: Rule, files: list[dict]) -> tuple[list[_Evidence], str, int]:
    """Collect de-duplicated evidence for one rule across all files."""
    evidence: list[_Evidence] = []
    seen_snippets: set[str] = set()
    matched_pattern = ""
    match_count = 0

    for file in files:
        content = file.get("content") or ""
        if not content:
            continue
        lines = content.splitlines()
        for i, line in enumerate(lines):
            for pattern in rule.patterns:
                found = pattern.search(line)
                if not found:
                    continue
                match_count += 1
                if not matched_pattern:
                    matched_pattern = found.group(0)[:_MATCHED_PATTERN_MAX]
                snippet = _snippet_for(lines, i)
                key = " ".join(snippet.split()).lower()  # whitespace-insensitive
                if key in seen_snippets:
                    break  # one evidence item per line; avoid near-duplicates
                seen_snippets.add(key)
                if len(evidence) < _MAX_EVIDENCE_PER_ISSUE:
                    evidence.append(
                        _Evidence(
                            source=file.get("filename", "uploaded file"),
                            line_or_section=f"line {i + 1}",
                            snippet=snippet,
                            reason=rule.reason,
                        )
                    )
                break  # stop after the first pattern that matches this line

    return evidence, matched_pattern, match_count


def detect(project: dict, files: list[dict]) -> list[dict]:
    """Run every rule over ``files`` and return ranked issue payloads.

    ``project``: ``{"stack": str, "cloud_provider": str}``.
    ``files``: ``[{"filename", "file_type", "content", "line_count"}, ...]``
    (``content`` must already be redacted — evidence is taken verbatim from it).
    """
    haystack = " ".join(
        [
            (project.get("stack") or ""),
            (project.get("cloud_provider") or ""),
            *(f.get("content") or "" for f in files),
        ]
    ).lower()

    issues: list[dict] = []
    for rule in RULES:
        if not _context_satisfied(rule, haystack):
            continue
        evidence, matched_pattern, match_count = _match_rule(rule, files)
        if not evidence:
            continue
        # Repeated matches modestly raise the hint, never to certainty.
        confidence = min(rule.confidence + 0.03 * (match_count - 1), _MAX_CONFIDENCE_HINT)
        issues.append(
            {
                "issue_type": rule.issue_type,
                "severity": rule.severity,
                "confidence_hint": round(confidence, 4),
                "matched_pattern": matched_pattern,
                "evidence": [e.as_dict() for e in evidence],
            }
        )

    issues.sort(
        key=lambda issue: (
            _SEVERITY_RANK.get(issue["severity"], 99),
            -issue["confidence_hint"],
        )
    )
    return issues


def run_for_session(session: DebugSession) -> list[DetectedIssue]:
    """Detect issues for ``session`` and persist them as ``DetectedIssue`` rows.

    Idempotent: any previously detected issues for the session are cleared first,
    so re-analyzing never accumulates stale duplicates.
    """
    project = {
        "stack": session.project.stack,
        "cloud_provider": session.project.cloud_provider,
    }
    files = [
        {
            "filename": f.filename,
            "file_type": f.file_type,
            "content": f.content,
            "line_count": f.line_count,
        }
        for f in session.files.all()
    ]

    payloads = detect(project, files)

    session.detected_issues.all().delete()
    rows = [
        DetectedIssue(
            debug_session=session,
            issue_type=p["issue_type"],
            severity=p["severity"],
            confidence_hint=p["confidence_hint"],
            matched_pattern=p["matched_pattern"],
            evidence=p["evidence"],
        )
        for p in payloads
    ]
    return DetectedIssue.objects.bulk_create(rows)
