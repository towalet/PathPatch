"""Upload pipeline tests: redaction, file validation, and the upload endpoint.

Covers docs/AGENT_PLAN.md §9 exit criteria:
    - valid text files are stored *redacted*
    - invalid / binary / oversized files are rejected with clear errors
    - duplicates are handled predictably
    - secrets never reach the database
"""

from __future__ import annotations

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse
from rest_framework import status

from apps.diagnostics.models import UploadedFile
from apps.diagnostics.services import file_parser, redaction
from apps.diagnostics.services.file_parser import FileValidationError

from .factories import DebugSessionFactory

pytestmark = pytest.mark.django_db


def upload_url(session_id):
    return reverse("api:diagnostics:session-upload", kwargs={"session_id": session_id})


def text_file(name: str, body: bytes) -> SimpleUploadedFile:
    return SimpleUploadedFile(name, body, content_type="text/plain")


# ---------------------------------------------------------------------------
# Redaction (pure unit tests — no DB)
# ---------------------------------------------------------------------------


class TestRedaction:
    def test_database_url_assignment_preserves_var_name(self):
        result = redaction.redact("DATABASE_URL=postgres://admin:s3cr3tpw@db:5432/app")
        assert result.text == "DATABASE_URL=[REDACTED_DATABASE_URL]"
        assert result.count == 1
        assert "s3cr3tpw" not in result.text

    def test_redacts_provider_api_key(self):
        secret = "sk-" + "A" * 28
        result = redaction.redact(f"OPENAI_API_KEY={secret}")
        assert secret not in result.text
        assert "[REDACTED" in result.text

    def test_redacts_jwt(self):
        jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N"
        result = redaction.redact(f"Authorization token {jwt}")
        assert "[REDACTED_JWT]" in result.text
        assert jwt not in result.text

    def test_redacts_private_key_block(self):
        blob = (
            "-----BEGIN RSA PRIVATE KEY-----\n"
            "MIIEowIBAAKCAQEA1234567890\n"
            "-----END RSA PRIVATE KEY-----"
        )
        result = redaction.redact(blob)
        assert result.text == "[REDACTED_PRIVATE_KEY]"
        assert "MIIEow" not in result.text

    def test_redacts_aws_access_key(self):
        result = redaction.redact("aws_key = AKIAIOSFODNN7EXAMPLE")
        assert "AKIAIOSFODNN7EXAMPLE" not in result.text
        assert "[REDACTED_AWS_KEY]" in result.text

    def test_redacts_password_in_connection_url(self):
        result = redaction.redact("connecting to postgres://admin:hunter2@db:5432/app")
        assert "hunter2" not in result.text
        assert "postgres://admin:[REDACTED]@db:5432/app" in result.text

    def test_leaves_ordinary_logs_untouched(self):
        body = "LEVEL=info\nstarting server on port 8000\nServer ready"
        result = redaction.redact(body)
        assert result.text == body
        assert result.count == 0


# ---------------------------------------------------------------------------
# File parser / validation (pure unit tests — no DB)
# ---------------------------------------------------------------------------


class TestFileParser:
    @pytest.mark.parametrize(
        ("filename", "expected_type"),
        [
            ("deploy.log", "log"),
            ("error.txt", "text"),
            ("settings.py", "python"),
            ("Dockerfile", "dockerfile"),
            ("Procfile", "procfile"),
            ("package.json", "json"),
            ("requirements.txt", "requirements"),
            ("requirements-dev.txt", "requirements"),
            (".env.example", "env"),
            ("vite.config.ts", "typescript"),
            ("compose.yaml", "yaml"),
        ],
    )
    def test_classify_supported_types(self, filename, expected_type):
        assert file_parser.classify(filename) == expected_type

    def test_classify_rejects_unknown(self):
        assert file_parser.classify("photo.png") is None
        assert file_parser.classify("archive.zip") is None

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("../../etc/passwd", "passwd"),
            ("..\\..\\secret.log", "secret.log"),
            ("/var/log/deploy.log", "deploy.log"),
        ],
    )
    def test_sanitize_filename_strips_paths(self, raw, expected):
        assert file_parser.sanitize_filename(raw) == expected

    def test_parse_rejects_unsupported_extension(self):
        with pytest.raises(FileValidationError):
            file_parser.parse_upload("image.png", b"data", max_bytes=1024)

    def test_parse_rejects_binary_content(self):
        with pytest.raises(FileValidationError, match="binary"):
            file_parser.parse_upload("deploy.log", b"text\x00more", max_bytes=1024)

    def test_parse_rejects_oversize(self):
        with pytest.raises(FileValidationError, match="per-file limit"):
            file_parser.parse_upload("deploy.log", b"x" * 100, max_bytes=10)

    def test_parse_rejects_non_utf8(self):
        with pytest.raises(FileValidationError, match="UTF-8"):
            file_parser.parse_upload("deploy.log", b"\xff\xfe\xfa", max_bytes=1024)

    def test_parse_success(self):
        parsed = file_parser.parse_upload("deploy.log", b"hello\nworld", max_bytes=1024)
        assert parsed.filename == "deploy.log"
        assert parsed.file_type == "log"
        assert parsed.text == "hello\nworld"


# ---------------------------------------------------------------------------
# Upload endpoint (integration)
# ---------------------------------------------------------------------------


class TestUploadEndpoint:
    def test_requires_authentication(self, api_client, user):
        session = DebugSessionFactory(project__user=user)
        resp = api_client.post(upload_url(session.id))
        assert resp.status_code == status.HTTP_401_UNAUTHORIZED

    def test_stores_file_redacted(self, auth_client, user):
        session = DebugSessionFactory(project__user=user)
        body = b"DATABASE_URL=postgres://admin:s3cr3tpw@db:5432/app\nboot failed\n"
        resp = auth_client.post(
            upload_url(session.id),
            {"files": [text_file("deploy.log", body)]},
            format="multipart",
        )
        assert resp.status_code == status.HTTP_201_CREATED
        assert len(resp.data["uploaded"]) == 1
        assert resp.data["errors"] == []
        item = resp.data["uploaded"][0]
        assert item["filename"] == "deploy.log"
        assert item["redaction_count"] >= 1
        # Raw content is never returned in the metadata payload.
        assert "content" not in item

        stored = UploadedFile.objects.get(id=item["id"])
        assert "s3cr3tpw" not in stored.content
        assert "[REDACTED_DATABASE_URL]" in stored.content

    def test_accepts_pasted_text(self, auth_client, user):
        session = DebugSessionFactory(project__user=user)
        resp = auth_client.post(
            upload_url(session.id),
            {"pasted_text": "ModuleNotFoundError: No module named 'django'"},
            format="json",
        )
        assert resp.status_code == status.HTTP_201_CREATED
        assert resp.data["uploaded"][0]["filename"] == "pasted-error.log"

    def test_rejects_unsupported_and_binary(self, auth_client, user):
        session = DebugSessionFactory(project__user=user)
        resp = auth_client.post(
            upload_url(session.id),
            {
                "files": [
                    text_file("photo.png", b"PNGDATA"),
                    text_file("core.log", b"ok\x00binary"),
                ]
            },
            format="multipart",
        )
        assert resp.status_code == status.HTTP_400_BAD_REQUEST
        assert resp.data["uploaded"] == []
        assert len(resp.data["errors"]) == 2
        assert not UploadedFile.objects.filter(debug_session=session).exists()

    def test_partial_success_reports_per_file(self, auth_client, user):
        session = DebugSessionFactory(project__user=user)
        resp = auth_client.post(
            upload_url(session.id),
            {
                "files": [
                    text_file("good.log", b"all good here"),
                    text_file("bad.png", b"nope"),
                ]
            },
            format="multipart",
        )
        assert resp.status_code == status.HTTP_201_CREATED
        assert len(resp.data["uploaded"]) == 1
        assert len(resp.data["errors"]) == 1
        assert resp.data["errors"][0]["filename"] == "bad.png"

    def test_duplicate_content_is_rejected(self, auth_client, user):
        session = DebugSessionFactory(project__user=user)
        body = b"identical content"
        first = auth_client.post(
            upload_url(session.id),
            {"files": [text_file("a.log", body)]},
            format="multipart",
        )
        assert first.status_code == status.HTTP_201_CREATED

        second = auth_client.post(
            upload_url(session.id),
            {"files": [text_file("a.log", body)]},
            format="multipart",
        )
        assert second.status_code == status.HTTP_400_BAD_REQUEST
        assert "Duplicate" in second.data["errors"][0]["error"]
        assert UploadedFile.objects.filter(debug_session=session).count() == 1

    def test_per_file_size_limit_enforced(self, auth_client, user, settings):
        settings.PATCHPATH_MAX_FILE_BYTES = 16
        session = DebugSessionFactory(project__user=user)
        resp = auth_client.post(
            upload_url(session.id),
            {"files": [text_file("big.log", b"x" * 64)]},
            format="multipart",
        )
        assert resp.status_code == status.HTTP_400_BAD_REQUEST
        assert "per-file limit" in resp.data["errors"][0]["error"]

    def test_session_budget_enforced(self, auth_client, user, settings):
        settings.PATCHPATH_MAX_SESSION_BYTES = 20
        session = DebugSessionFactory(project__user=user)
        resp = auth_client.post(
            upload_url(session.id),
            {"files": [text_file("big.log", b"x" * 64)]},
            format="multipart",
        )
        assert resp.status_code == status.HTTP_400_BAD_REQUEST
        assert "Session upload limit" in resp.data["errors"][0]["error"]

    def test_no_input_is_rejected(self, auth_client, user):
        session = DebugSessionFactory(project__user=user)
        resp = auth_client.post(upload_url(session.id), {}, format="json")
        assert resp.status_code == status.HTTP_400_BAD_REQUEST
        assert "detail" in resp.data

    def test_cannot_upload_to_foreign_session(self, auth_client, other_user):
        session = DebugSessionFactory(project__user=other_user)
        resp = auth_client.post(
            upload_url(session.id),
            {"files": [text_file("a.log", b"hello")]},
            format="multipart",
        )
        assert resp.status_code == status.HTTP_404_NOT_FOUND
        assert not UploadedFile.objects.filter(debug_session=session).exists()
