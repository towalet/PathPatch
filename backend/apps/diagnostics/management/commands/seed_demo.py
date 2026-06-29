"""Seed a recruiter-ready PatchPath demo.

`python manage.py seed_demo`

The command is deterministic and offline-safe: it creates a demo user, project,
uploaded evidence, deterministic rule matches, and a completed diagnosis report
without calling the AI service. Fresh clones stay demoable when OPENAI_API_KEY is
unset.
"""

from __future__ import annotations

from pathlib import Path

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from apps.diagnostics.models import DebugSession, DiagnosisReport, Project, SessionStatus
from apps.diagnostics.services import file_intake, rule_detector

DEMO_EMAIL = "demo@patchpath.dev"
DEMO_PASSWORD = "PatchPathDemo123!"
DEMO_PROJECT_NAME = "Django Render API"
DEMO_ERROR_SUMMARY = (
    "Render deployment exits during Django startup before the service binds to a port."
)
DEMO_FILES = [
    "render-django-database-url.log",
    "env.example.txt",
    "settings-snippet.py",
]


def _samples_dir() -> Path:
    return Path(settings.BASE_DIR).parent / "samples"


def _read_demo_files() -> list[tuple[str, bytes]]:
    samples = _samples_dir()
    missing = [name for name in DEMO_FILES if not (samples / name).exists()]
    if missing:
        raise FileNotFoundError(f"Missing demo sample file(s): {', '.join(missing)} in {samples}")
    return [(name, (samples / name).read_bytes()) for name in DEMO_FILES]


def _report_fields() -> dict:
    return {
        "root_cause": (
            "DATABASE_URL is most likely missing from the Render service "
            "environment during Django startup."
        ),
        "confidence_score": 0.86,
        "severity": "high",
        "detected_stack": ["Django", "PostgreSQL"],
        "detected_cloud_provider": "Render",
        "explanation": (
            "The uploaded Render log shows Django raising KeyError for "
            "DATABASE_URL while loading production database settings. The "
            "settings snippet corroborates that startup reads "
            "os.environ['DATABASE_URL'], so the process exits before the web "
            "service can bind to a port."
        ),
        "evidence_json": [
            {
                "source": "render-django-database-url.log",
                "line_or_section": "line 9",
                "reason": "The deploy log shows KeyError: 'DATABASE_URL' during startup.",
            },
            {
                "source": "settings-snippet.py",
                "line_or_section": "DATABASES setting",
                "reason": "Production settings read os.environ['DATABASE_URL'] directly.",
            },
            {
                "source": "env.example.txt",
                "line_or_section": "DATABASE_URL comment",
                "reason": "The example environment marks DATABASE_URL as required for production.",
            },
        ],
        "recommended_fix": (
            "Create or attach a PostgreSQL database for the Render service, set "
            "DATABASE_URL in that service's environment variables, and redeploy. "
            "If the value already exists in another Render service or group, "
            "confirm it is shared with this web service."
        ),
        "commands_json": [
            "render services env set DATABASE_URL=<postgres-connection-string>",
            "python manage.py check --deploy",
            "python manage.py migrate --check",
        ],
        "verification_checklist_json": [
            "Render service environment includes DATABASE_URL for the web service.",
            "A redeploy starts without KeyError: 'DATABASE_URL'.",
            "The process binds to the expected platform port.",
            "Health endpoint returns 200 after deployment.",
        ],
        "missing_information_json": [
            "The live Render environment variable list was not uploaded.",
            "The database provider/connection string has not been verified from the dashboard.",
        ],
        "possible_risks_json": [
            "Changing DATABASE_URL can point the app at a different database if copied incorrectly.",
            "After the variable is fixed, migrations or network access may reveal a second deployment issue.",
        ],
        "model_name": "seed-demo",
        "prompt_tokens": None,
        "completion_tokens": None,
    }


class Command(BaseCommand):
    help = "Seed demo data (user, project, diagnosed session)."

    @transaction.atomic
    def handle(self, *args, **options) -> None:
        User = get_user_model()

        user, _ = User.objects.update_or_create(
            email=DEMO_EMAIL,
            defaults={"name": "PatchPath Demo"},
        )
        user.set_password(DEMO_PASSWORD)
        user.save(update_fields=["password", "name"])

        project, _ = Project.objects.update_or_create(
            user=user,
            name=DEMO_PROJECT_NAME,
            defaults={"stack": "Django, PostgreSQL", "cloud_provider": "Render"},
        )

        session = (
            DebugSession.objects.filter(project=project, error_summary=DEMO_ERROR_SUMMARY)
            .order_by("created_at")
            .first()
        )
        if session is None:
            session = DebugSession.objects.create(
                project=project,
                error_summary=DEMO_ERROR_SUMMARY,
            )

        DiagnosisReport.objects.filter(debug_session=session).delete()
        session.detected_issues.all().delete()
        session.files.all().delete()

        ingest_result = file_intake.ingest(session, _read_demo_files())
        if ingest_result.errors:
            details = "; ".join(
                f"{item['filename']}: {item['error']}" for item in ingest_result.errors
            )
            raise RuntimeError(f"Demo evidence failed validation: {details}")

        issues = rule_detector.run_for_session(session)
        now = timezone.now()
        DiagnosisReport.objects.update_or_create(
            debug_session=session,
            defaults=_report_fields(),
        )
        session.status = SessionStatus.COMPLETED
        session.failure_reason = ""
        session.analysis_started_at = now
        session.analysis_completed_at = now
        session.save(
            update_fields=[
                "status",
                "failure_reason",
                "analysis_started_at",
                "analysis_completed_at",
                "updated_at",
            ]
        )

        self.stdout.write(
            self.style.SUCCESS(
                "Seeded PatchPath demo: "
                f"{DEMO_EMAIL} / {DEMO_PASSWORD}; "
                f"project={project.id}; session={session.id}; "
                f"files={len(ingest_result.stored)}; issues={len(issues)}"
            )
        )
