"""
Evidence bundle builder (docs/AGENT_PLAN.md §11).

Converts uploaded files + detected issues into a compact, LLM-safe payload. The
model never sees whole logs — only filename/line-labelled snippets the detector
already surfaced, plus a few error-looking lines no rule matched, plus hints
about context that is *missing*. The whole bundle is trimmed to stay under
``PATCHPATH_EVIDENCE_CHAR_BUDGET`` so a giant log can never blow the prompt.
"""

from __future__ import annotations

import json
import re

# Files whose absence materially weakens a diagnosis — surfaced so the model can
# say "X was not uploaded" instead of guessing.
_USEFUL_CONTEXT_FILES = {
    "settings.py": "Django settings (settings.py) was not uploaded.",
    "package.json": "package.json was not uploaded.",
    "requirements.txt": "requirements.txt was not uploaded.",
    ".env.example": ".env.example was not uploaded.",
    "dockerfile": "Dockerfile was not uploaded.",
    "procfile": "Procfile was not uploaded.",
}

# Lines that look like failures but no rule claimed — weak signal worth showing.
_ERROR_HINT = re.compile(
    r"\b(error|exception|traceback|failed|fatal|cannot|denied|refused)\b",
    re.IGNORECASE,
)

_MAX_TOP_EVIDENCE = 12
_MAX_UNMATCHED_LINES = 8
_UNMATCHED_LINE_CAP = 200


def _file_summaries(files: list[dict]) -> list[dict]:
    return [
        {
            "filename": f.get("filename", "uploaded file"),
            "file_type": f.get("file_type", "text"),
            "line_count": f.get("line_count", 0),
        }
        for f in files
    ]


def _top_evidence(detected_issues: list[dict]) -> list[dict]:
    """Flatten issue evidence into a ranked, deduplicated citation list."""
    seen: set[tuple] = set()
    flat: list[dict] = []
    for issue in detected_issues:
        for ev in issue.get("evidence", []):
            key = (ev.get("source"), ev.get("line_or_section"), ev.get("snippet"))
            if key in seen:
                continue
            seen.add(key)
            flat.append(
                {
                    "issue_type": issue["issue_type"],
                    "source": ev.get("source", ""),
                    "line_or_section": ev.get("line_or_section", ""),
                    "snippet": ev.get("snippet", ""),
                    "reason": ev.get("reason", ""),
                }
            )
    return flat


def _matched_snippets(detected_issues: list[dict]) -> set[str]:
    out: set[str] = set()
    for issue in detected_issues:
        for ev in issue.get("evidence", []):
            for line in (ev.get("snippet", "")).splitlines():
                stripped = line.strip()
                if stripped:
                    out.add(stripped)
    return out


def _unmatched_error_lines(files: list[dict], matched: set[str]) -> list[str]:
    """Error-looking lines that no rule already cited (capped + de-duplicated)."""
    lines: list[str] = []
    seen: set[str] = set()
    for f in files:
        for raw in (f.get("content") or "").splitlines():
            line = raw.strip()
            if not line or line in matched or line in seen:
                continue
            if _ERROR_HINT.search(line):
                seen.add(line)
                lines.append(f"{f.get('filename', '?')}: {line[:_UNMATCHED_LINE_CAP]}")
                if len(lines) >= _MAX_UNMATCHED_LINES:
                    return lines
    return lines


def _known_missing_context(files: list[dict], detected_issues: list[dict]) -> list[str]:
    present = {f.get("filename", "").lower() for f in files}
    hints = [
        note
        for key, note in _USEFUL_CONTEXT_FILES.items()
        if not any(key in name for name in present)
    ]
    if not detected_issues:
        hints.append(
            "No deterministic rules matched the uploaded evidence; the diagnosis "
            "is weakly supported."
        )
    return hints


def _measure(bundle: dict) -> int:
    return len(json.dumps(bundle, ensure_ascii=False))


def build_bundle(
    project: dict,
    files: list[dict],
    detected_issues: list[dict],
    *,
    char_budget: int,
) -> dict:
    """Assemble the evidence bundle, trimmed to ``char_budget`` characters.

    Trimming is progressive and drops the least-load-bearing material first:
    unmatched error lines, then the long tail of top evidence.
    """
    matched = _matched_snippets(detected_issues)
    bundle = {
        "project_metadata": {
            "stack": project.get("stack", ""),
            "cloud_provider": project.get("cloud_provider", ""),
        },
        "uploaded_file_summaries": _file_summaries(files),
        "detected_issues": [
            {
                "issue_type": i["issue_type"],
                "severity": i["severity"],
                "confidence_hint": i["confidence_hint"],
                "matched_pattern": i.get("matched_pattern", ""),
            }
            for i in detected_issues
        ],
        "top_evidence": _top_evidence(detected_issues)[:_MAX_TOP_EVIDENCE],
        "unmatched_error_lines": _unmatched_error_lines(files, matched),
        "known_missing_context": _known_missing_context(files, detected_issues),
    }

    # Progressive trim until we fit. Order: shed weak signal before strong.
    while _measure(bundle) > char_budget and bundle["unmatched_error_lines"]:
        bundle["unmatched_error_lines"].pop()
    while _measure(bundle) > char_budget and len(bundle["top_evidence"]) > 1:
        bundle["top_evidence"].pop()

    return bundle


def known_sources(files: list[dict]) -> set[str]:
    """Filenames the model is allowed to cite in evidence."""
    return {f.get("filename", "") for f in files if f.get("filename")}
