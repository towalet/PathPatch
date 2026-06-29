"""
File parsing & upload validation (docs/AGENT_PLAN.md §9 + §2).

Accepts text-like files only and rejects binary by extension *and* content
sniffing. Filenames are sanitised (path stripped) before anything else, so a
crafted ``../../etc/passwd`` name can never escape its basename. Size limits and
the duplicate/budget logic live one layer up in ``file_intake``; this module is
the pure, side-effect-free validator + classifier.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

# Normalised extension -> file_type label.
_EXTENSION_TYPES = {
    ".log": "log",
    ".txt": "text",
    ".json": "json",
    ".yaml": "yaml",
    ".yml": "yaml",
    ".toml": "toml",
    ".ini": "config",
    ".cfg": "config",
    ".conf": "config",
    ".py": "python",
    ".js": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
}

# Exact (lowercased) filenames that are allowed without a recognised extension.
_SPECIAL_FILENAMES = {
    "dockerfile": "dockerfile",
    "procfile": "procfile",
    "package.json": "json",
    "makefile": "makefile",
}


class FileValidationError(ValueError):
    """Raised when an upload is not an acceptable text artifact."""


@dataclass(frozen=True)
class ParsedFile:
    """A validated, decoded upload ready for redaction + persistence."""

    filename: str
    file_type: str
    text: str


def sanitize_filename(filename: str) -> str:
    """Reduce a user-supplied name to a safe basename (no path components)."""
    # Normalise both separators, then take the basename.
    candidate = (filename or "").replace("\\", "/").strip()
    candidate = os.path.basename(candidate).strip()
    if not candidate or candidate in {".", ".."}:
        raise FileValidationError("File must have a valid name.")
    return candidate


def classify(filename: str) -> str | None:
    """Return the file_type for an allowed name, or None if unsupported."""
    lowered = filename.lower()

    if lowered in _SPECIAL_FILENAMES:
        return _SPECIAL_FILENAMES[lowered]
    # requirements.txt, requirements-dev.txt, ...
    if lowered.startswith("requirements") and lowered.endswith(".txt"):
        return "requirements"
    # .env, .env.example, .env.local, ... (stored redacted regardless)
    if lowered == ".env" or ".env." in lowered or lowered.endswith(".env"):
        return "env"

    _, ext = os.path.splitext(lowered)
    return _EXTENSION_TYPES.get(ext)


def parse_upload(filename: str, raw: bytes, *, max_bytes: int) -> ParsedFile:
    """Validate and decode one upload.

    Raises ``FileValidationError`` with a user-facing message on any of:
    unsupported type, oversize, binary content, or non-UTF-8 text.
    """
    safe_name = sanitize_filename(filename)

    file_type = classify(safe_name)
    if file_type is None:
        raise FileValidationError(f"'{safe_name}' is not a supported text file type.")

    if len(raw) > max_bytes:
        limit_mb = max_bytes / (1024 * 1024)
        raise FileValidationError(f"'{safe_name}' exceeds the {limit_mb:.0f} MB per-file limit.")

    # Binary sniff: a NUL byte is the strongest signal of non-text content.
    if b"\x00" in raw:
        raise FileValidationError(f"'{safe_name}' appears to be a binary file.")

    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise FileValidationError(f"'{safe_name}' is not valid UTF-8 text.") from exc

    return ParsedFile(filename=safe_name, file_type=file_type, text=text)
