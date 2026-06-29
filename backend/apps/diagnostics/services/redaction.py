"""
Secret redaction.

Runs BEFORE content is stored and BEFORE any AI call (docs/AGENT_PLAN.md §9).
Returns redacted text plus a count of substitutions made.

Design: an ordered list of compiled rules, applied in sequence. Order matters —
structured/high-signal secrets (private keys, provider keys, JWTs) are redacted
first so the broad fallbacks (env assignments, URL credentials) don't double-count
or partially mangle them. Variable names are preserved where useful, e.g.

    DATABASE_URL=postgres://u:p@host/db   ->   DATABASE_URL=[REDACTED_DATABASE_URL]
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Env-style keys whose assigned value should be fully redacted (name preserved).
_SENSITIVE_KEY = (
    r"[A-Z0-9_]*"
    r"(?:SECRET|PASSWORD|PASSWD|PWD|TOKEN|API_?KEY|ACCESS_?KEY|PRIVATE_?KEY|"
    r"DATABASE_URL|DB_PASS(?:WORD)?|CREDENTIALS?|AUTH)"
    r"[A-Z0-9_]*"
)

_ENV_ASSIGNMENT = re.compile(
    r"""
    ^(?P<prefix>\s*(?:export\s+)?)
    (?P<key>"""
    + _SENSITIVE_KEY
    + r""")
    (?P<sep>\s*[:=]\s*)
    (?P<quote>["']?)
    (?P<value>[^\r\n]*?)
    (?P=quote)
    [ \t]*$
    """,
    re.IGNORECASE | re.MULTILINE | re.VERBOSE,
)


def _redact_env_assignment(match: re.Match) -> str:
    value = match.group("value")
    # Skip empty values and anything we've already redacted.
    if not value or value.startswith("[REDACTED"):
        return match.group(0)
    key = match.group("key")
    return f"{match.group('prefix')}{key}{match.group('sep')}[REDACTED_{key.upper()}]"


def _keep_first_group(replacement: str):
    """Build a replacement that preserves capture group 1 and appends a label."""

    def _repl(match: re.Match) -> str:
        return f"{match.group(1)}{replacement}"

    return _repl


@dataclass(frozen=True)
class _Rule:
    name: str
    pattern: re.Pattern
    replacement: object  # str or callable


# Order is significant (see module docstring).
_RULES: tuple[_Rule, ...] = (
    _Rule(
        "private_key",
        re.compile(
            r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?-----END [A-Z0-9 ]*PRIVATE KEY-----",
            re.DOTALL,
        ),
        "[REDACTED_PRIVATE_KEY]",
    ),
    _Rule(
        "provider_api_key",
        re.compile(
            r"\b(?:"
            r"sk-[A-Za-z0-9]{20,}"  # OpenAI
            r"|gh[pousr]_[A-Za-z0-9]{20,}"  # GitHub
            r"|xox[baprs]-[A-Za-z0-9-]{10,}"  # Slack
            r"|AIza[0-9A-Za-z_\-]{30,}"  # Google
            r"|(?:sk|pk|rk)_(?:live|test)_[A-Za-z0-9]{10,}"  # Stripe
            r")\b"
        ),
        "[REDACTED_API_KEY]",
    ),
    _Rule(
        "jwt",
        re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"),
        "[REDACTED_JWT]",
    ),
    _Rule(
        "aws_access_key",
        re.compile(r"\b(?:AKIA|ASIA|AGPA|AIDA|AROA)[0-9A-Z]{16}\b"),
        "[REDACTED_AWS_KEY]",
    ),
    _Rule(
        "env_assignment",
        _ENV_ASSIGNMENT,
        _redact_env_assignment,
    ),
    _Rule(
        "url_credentials",
        re.compile(r"\b([a-zA-Z][a-zA-Z0-9+.\-]*://[^:@\s/]+):[^@\s/]+@"),
        _keep_first_group(":[REDACTED]@"),
    ),
    _Rule(
        "bearer_token",
        re.compile(r"(?i)\b(bearer)\s+[A-Za-z0-9._~+/\-]{10,}=*"),
        _keep_first_group(" [REDACTED_TOKEN]"),
    ),
)


@dataclass(frozen=True)
class RedactionResult:
    """Outcome of redacting a blob of text."""

    text: str
    count: int


def redact(text: str) -> RedactionResult:
    """Strip secrets from ``text`` and report how many were removed."""
    total = 0
    for rule in _RULES:
        text, n = rule.pattern.subn(rule.replacement, text)
        total += n
    return RedactionResult(text=text, count=total)
