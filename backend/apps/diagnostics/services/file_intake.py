"""
Upload intake orchestration (docs/AGENT_PLAN.md §4 + §9).

Ties the upload pipeline together for one session:

    parse/validate -> redact secrets -> de-duplicate -> enforce session budget
    -> persist UploadedFile

Partial success is intentional: each item succeeds or fails independently, and
the caller receives both the stored files and per-item errors. Only redacted
content is ever written to the database.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

from django.conf import settings
from django.db import IntegrityError

from ..models import DebugSession, UploadedFile
from . import file_parser, redaction


@dataclass
class IngestResult:
    """Outcome of an upload batch."""

    stored: list[UploadedFile] = field(default_factory=list)
    errors: list[dict] = field(default_factory=list)

    def add_error(self, filename: str, message: str) -> None:
        self.errors.append({"filename": filename, "error": message})


def _session_bytes_used(session: DebugSession) -> int:
    total = 0
    for size in session.files.values_list("size_bytes", flat=True):
        total += size
    return total


def ingest(session: DebugSession, items: list[tuple[str, bytes]]) -> IngestResult:
    """Validate, redact, and persist a batch of (filename, raw_bytes) uploads.

    ``items`` already includes any pasted text rendered as a synthetic upload.
    """
    max_file = settings.PATCHPATH_MAX_FILE_BYTES
    max_session = settings.PATCHPATH_MAX_SESSION_BYTES

    result = IngestResult()
    used = _session_bytes_used(session)
    # Hashes already attached to this session, plus ones added in this batch.
    seen_hashes = set(session.files.values_list("content_sha256", flat=True))

    for filename, raw in items:
        try:
            parsed = file_parser.parse_upload(filename, raw, max_bytes=max_file)
        except file_parser.FileValidationError as exc:
            result.add_error(filename, str(exc))
            continue

        redacted = redaction.redact(parsed.text)
        content = redacted.text
        size_bytes = len(content.encode("utf-8"))
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()

        if digest in seen_hashes:
            result.add_error(
                parsed.filename,
                "Duplicate of a file already uploaded to this session.",
            )
            continue

        if used + size_bytes > max_session:
            limit_mb = max_session / (1024 * 1024)
            result.add_error(
                parsed.filename,
                f"Session upload limit ({limit_mb:.0f} MB total) exceeded.",
            )
            continue

        try:
            stored = UploadedFile.objects.create(
                debug_session=session,
                filename=parsed.filename,
                file_type=parsed.file_type,
                content=content,
                content_sha256=digest,
                size_bytes=size_bytes,
                line_count=content.count("\n") + 1 if content else 0,
                redaction_count=redacted.count,
            )
        except IntegrityError:
            # Concurrent duplicate slipped past the in-memory check.
            result.add_error(
                parsed.filename,
                "Duplicate of a file already uploaded to this session.",
            )
            continue

        seen_hashes.add(digest)
        used += size_bytes
        result.stored.append(stored)

    return result
