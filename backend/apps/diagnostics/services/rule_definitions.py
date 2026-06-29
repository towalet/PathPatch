"""
Rule registry for the deterministic detector (docs/AGENT_PLAN.md §10).

Rules are declared as ``Rule`` dataclasses with regexes compiled once at import.
The detector ([rule_detector.py]) walks each uploaded file line-by-line and lets
every rule whose patterns match contribute a ``DetectedIssue`` with evidence.

Two design choices keep false positives down:

* Patterns encode the *error signal*, not bare keywords — e.g. ``DATABASE_URL``
  appears harmlessly in a committed ``.env.example``, so the rule matches
  ``KeyError: 'DATABASE_URL'`` / ``Set the DATABASE_URL environment variable``
  instead.
* Provider-specific rules carry a ``context`` gate: they only fire when a
  provider keyword is present in the project metadata or an uploaded file, so a
  generic Django traceback never trips the Vercel/Render rules.

Overlap between rules is intentional and realistic: one log can legitimately
surface several candidate issues. Ranking and the single root-cause call are
left to the AI layer (Phase 6); the detector only surfaces evidence-backed
candidates.

MVP rule set: missing_env_var, missing_database_url, port_binding_issue,
missing_python_dependency, missing_node_dependency, npm_build_failure,
cors_error, django_staticfiles_issue, postgres_connection_refused,
docker_build_failed, wrong_start_command, collectstatic_failed,
vercel_build_command_issue, render_railway_env_mismatch.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..models import Severity


@dataclass(frozen=True)
class Rule:
    """A single deterministic detection rule.

    A rule *fires* when any pattern in ``patterns`` matches a line in an uploaded
    file, provided any ``context`` gate is satisfied. ``confidence`` is the base
    ``confidence_hint`` for the resulting issue (the detector nudges it up a
    little when a rule matches repeatedly, but always keeps it ``< 1.0``).
    """

    issue_type: str
    severity: str
    confidence: float
    reason: str
    patterns: tuple[re.Pattern, ...]
    # If non-empty, at least one token must appear (case-insensitively) in the
    # project metadata or some uploaded file for the rule to fire. Used to gate
    # provider-specific rules so they never trip on unrelated logs.
    context: tuple[str, ...] = field(default=())


def _compile(*patterns: str) -> tuple[re.Pattern, ...]:
    return tuple(re.compile(p, re.IGNORECASE) for p in patterns)


# Order is presentation-only; the detector re-sorts by severity then confidence.
RULES: tuple[Rule, ...] = (
    Rule(
        issue_type="missing_database_url",
        severity=Severity.HIGH,
        confidence=0.82,
        reason="Startup failed while reading DATABASE_URL — the variable is "
        "most likely unset in the deploy environment.",
        patterns=_compile(
            r"KeyError:\s*['\"]?DATABASE_URL",
            r"Set the DATABASE_URL environment variable",
            r"\bNo DATABASE_URL\b",
            r"\bdj_database_url\b",
            r"DATABASE_URL.{0,40}(?:not set|is not set|missing|undefined)",
        ),
    ),
    Rule(
        issue_type="missing_env_var",
        severity=Severity.HIGH,
        confidence=0.7,
        reason="A required environment variable was missing at startup.",
        patterns=_compile(
            r"\bImproperlyConfigured\b",
            r"Missing required environment variable",
            r"Environment variable\b.{0,60}?(?:not set|is not set|missing|undefined)",
            r"KeyError:\s*['\"][A-Z][A-Z0-9_]{2,}['\"]",
        ),
    ),
    Rule(
        issue_type="port_binding_issue",
        severity=Severity.HIGH,
        confidence=0.78,
        reason="The platform could not detect an open port — the app is most "
        "likely not binding to 0.0.0.0:$PORT.",
        patterns=_compile(
            r"No open ports detected",
            r"failed to detect (?:an )?open port",
            r"Bind your service to 0\.0\.0\.0",
            r"bind\b.{0,40}\$PORT",
            r"address already in use",
            r"EADDRINUSE",
        ),
    ),
    Rule(
        issue_type="missing_python_dependency",
        severity=Severity.HIGH,
        confidence=0.8,
        reason="A Python import failed — the package is most likely missing " "from requirements.",
        patterns=_compile(
            r"\bModuleNotFoundError\b",
            r"No module named ['\"]?[\w.]+",
            r"\bImportError\b",
        ),
    ),
    Rule(
        issue_type="missing_node_dependency",
        severity=Severity.HIGH,
        confidence=0.8,
        reason="A Node module could not be resolved — it is most likely not "
        "installed or not in package.json dependencies.",
        patterns=_compile(
            r"Cannot find module ['\"]?[\w@./-]+",
            r"\bERR_MODULE_NOT_FOUND\b",
            r"\bMODULE_NOT_FOUND\b",
            r"Module not found: Error",
        ),
    ),
    Rule(
        issue_type="npm_build_failure",
        severity=Severity.MEDIUM,
        confidence=0.6,
        reason="The frontend/Node build step failed.",
        patterns=_compile(
            r"npm ERR!",
            r"\bELIFECYCLE\b",
            r"\bvite build\b.{0,40}(?:failed|error)",
            r"react-scripts build.{0,40}(?:failed|error)",
            r"Build failed with \d+ error",
        ),
    ),
    Rule(
        issue_type="cors_error",
        severity=Severity.MEDIUM,
        confidence=0.65,
        reason="A browser request was blocked by CORS — the API is most likely "
        "not allowing the frontend origin.",
        patterns=_compile(
            r"blocked by CORS policy",
            r"No 'Access-Control-Allow-Origin' header",
            r"has been blocked by CORS",
        ),
    ),
    Rule(
        issue_type="django_staticfiles_issue",
        severity=Severity.MEDIUM,
        confidence=0.55,
        reason="Django could not resolve static files at runtime.",
        patterns=_compile(
            r"Missing staticfiles manifest entry",
            r"\bManifestStaticFilesStorage\b",
            r"\bWhiteNoise\b.{0,40}(?:error|not|fail)",
            r"You're using the staticfiles app without having set",
        ),
    ),
    Rule(
        issue_type="postgres_connection_refused",
        severity=Severity.HIGH,
        confidence=0.78,
        reason="The app could not reach PostgreSQL — the host/port is most "
        "likely wrong or the database is unreachable.",
        patterns=_compile(
            r"could not connect to server",
            r"connection refused",
            r"\bECONNREFUSED\b",
            r"Is the server running on host",
            r"OperationalError.{0,60}(?:connect|connection)",
        ),
    ),
    Rule(
        issue_type="docker_build_failed",
        severity=Severity.HIGH,
        confidence=0.75,
        reason="The Docker image build failed.",
        patterns=_compile(
            r"failed to solve",
            r"executor failed running",
            r"COPY failed",
            r"returned a non-zero code",
            r"failed to compute cache key",
        ),
    ),
    Rule(
        issue_type="wrong_start_command",
        severity=Severity.HIGH,
        confidence=0.7,
        reason="The service started but never came up — the start command is "
        "most likely wrong or the process exited immediately.",
        patterns=_compile(
            r"Application failed to respond",
            r"gunicorn: (?:command )?not found",
            r"command not found.{0,40}(?:gunicorn|uvicorn|node|npm)",
            r"\bExited with status 1\b",
            r"Process exited with status",
        ),
    ),
    Rule(
        issue_type="collectstatic_failed",
        severity=Severity.MEDIUM,
        confidence=0.6,
        reason="The collectstatic build step failed.",
        patterns=_compile(
            r"collectstatic.{0,60}(?:error|failed|Traceback)",
            r"Error.{0,40}collectstatic",
            r"Post-processing ['\"].+['\"] failed",
            r"ValueError: Missing staticfiles",
        ),
    ),
    Rule(
        issue_type="vercel_build_command_issue",
        severity=Severity.MEDIUM,
        confidence=0.6,
        reason="The Vercel build/output configuration looks wrong for this " "project.",
        context=("vercel",),
        patterns=_compile(
            r"No Output Directory named",
            r"Output Directory.{0,40}(?:not found|missing|empty)",
            r"Build Command.{0,40}(?:failed|exited|not)",
            r"Error: No (?:Output Directory|framework)",
        ),
    ),
    Rule(
        issue_type="render_railway_env_mismatch",
        severity=Severity.MEDIUM,
        confidence=0.6,
        reason="A Render/Railway service is missing an environment/service "
        "variable that the app expects.",
        context=("render", "railway"),
        patterns=_compile(
            r"\bservice variable\b",
            r"\bshared variable\b",
            r"environment variable\b.{0,60}?(?:not set|missing|undefined|not defined)",
        ),
    ),
)
