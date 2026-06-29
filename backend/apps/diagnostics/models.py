"""
Diagnostics domain models (see docs/AGENT_PLAN.md §7 "Database Schema").

Domain shape:
    Project          : a user's app/repo being diagnosed
    DebugSession     : one analysis run against uploaded evidence
    UploadedFile     : a redacted text artifact attached to a session
    DetectedIssue    : a deterministic rule match with evidence snippets
    DiagnosisReport  : the schema-validated AI root-cause report (1:1 session)

Conventions:
    - UUID primary keys for externally exposed records (opaque IDs in URLs).
    - TIMESTAMPTZ via DateTimeField (USE_TZ=True).
    - Ownership flows Project.user -> DebugSession.project -> children; every model
      exposes ``owner_id`` so permissions can resolve the owner without a query
      when the chain is already select_related().
    - Read access is funnelled through ``for_user()`` queryset methods so views and
      selectors cannot accidentally leak cross-tenant rows.
"""

from __future__ import annotations

import uuid

from django.conf import settings
from django.db import models
from django.db.models import Q
from django.db.models.functions import Lower

# ---------------------------------------------------------------------------
# Choices
# ---------------------------------------------------------------------------


class Severity(models.TextChoices):
    """Shared severity scale for detected issues and reports."""

    LOW = "low", "Low"
    MEDIUM = "medium", "Medium"
    HIGH = "high", "High"


class SessionStatus(models.TextChoices):
    """Lifecycle of a single analysis run."""

    PENDING = "pending", "Pending"
    ANALYZING = "analyzing", "Analyzing"
    COMPLETED = "completed", "Completed"
    FAILED = "failed", "Failed"


# Ordered worst-first so detected issues can be ranked deterministically in SQL.
_SEVERITY_RANK = {Severity.HIGH: 0, Severity.MEDIUM: 1, Severity.LOW: 2}


# ---------------------------------------------------------------------------
# Abstract bases
# ---------------------------------------------------------------------------


class UUIDModel(models.Model):
    """Opaque UUID primary key for externally exposed records."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    class Meta:
        abstract = True


class TimeStampedModel(models.Model):
    """Adds ``created_at`` / ``updated_at`` audit timestamps."""

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


# ---------------------------------------------------------------------------
# Project
# ---------------------------------------------------------------------------


class ProjectQuerySet(models.QuerySet):
    def for_user(self, user) -> ProjectQuerySet:
        """Only projects owned by ``user``."""
        return self.filter(user=user)

    def with_session_stats(self) -> ProjectQuerySet:
        """Annotate lightweight counts used by list/detail views."""
        return self.annotate(
            session_count=models.Count("sessions", distinct=True),
            latest_session_at=models.Max("sessions__created_at"),
        )


class Project(UUIDModel, TimeStampedModel):
    """An application/repository a user is diagnosing."""

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="projects",
    )
    name = models.CharField(max_length=200)
    stack = models.CharField(max_length=255, blank=True)
    cloud_provider = models.CharField(max_length=100, blank=True)

    objects = ProjectQuerySet.as_manager()

    class Meta:
        db_table = "diagnostics_project"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["user", "-created_at"]),
        ]
        constraints = [
            # Case-insensitive unique project name per user (PG functional unique).
            models.UniqueConstraint(
                "user",
                Lower("name"),
                name="uniq_project_name_per_user_ci",
            ),
        ]

    def __str__(self) -> str:
        return self.name

    @property
    def owner_id(self):
        return self.user_id


# ---------------------------------------------------------------------------
# DebugSession
# ---------------------------------------------------------------------------


class DebugSessionQuerySet(models.QuerySet):
    def for_user(self, user) -> DebugSessionQuerySet:
        """Only sessions whose project is owned by ``user``."""
        return self.filter(project__user=user)


class DebugSession(UUIDModel, TimeStampedModel):
    """One analysis run against a set of uploaded evidence."""

    project = models.ForeignKey(
        Project,
        on_delete=models.CASCADE,
        related_name="sessions",
    )
    status = models.CharField(
        max_length=20,
        choices=SessionStatus.choices,
        default=SessionStatus.PENDING,
    )
    error_summary = models.TextField(blank=True)
    analysis_started_at = models.DateTimeField(null=True, blank=True)
    analysis_completed_at = models.DateTimeField(null=True, blank=True)
    failure_reason = models.TextField(blank=True)

    objects = DebugSessionQuerySet.as_manager()

    class Meta:
        db_table = "diagnostics_debug_session"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["project", "-created_at"]),
            models.Index(fields=["status"]),
        ]
        constraints = [
            models.CheckConstraint(
                check=Q(status__in=SessionStatus.values),
                name="debug_session_status_valid",
            ),
        ]

    def __str__(self) -> str:
        return f"Session {self.pk} ({self.status})"

    @property
    def owner_id(self):
        # project is select_related() on every read path, so this is free.
        return self.project.user_id


# ---------------------------------------------------------------------------
# UploadedFile
# ---------------------------------------------------------------------------


class UploadedFileQuerySet(models.QuerySet):
    def for_user(self, user) -> UploadedFileQuerySet:
        return self.filter(debug_session__project__user=user)


class UploadedFile(UUIDModel):
    """A redacted text artifact attached to a session.

    ``content`` stores the *redacted* text (secrets stripped before persistence);
    raw uploads are never written to the database.
    """

    debug_session = models.ForeignKey(
        DebugSession,
        on_delete=models.CASCADE,
        related_name="files",
    )
    filename = models.CharField(max_length=255)
    file_type = models.CharField(max_length=50)
    content = models.TextField()
    content_sha256 = models.CharField(max_length=64, db_index=True)
    size_bytes = models.PositiveIntegerField()
    line_count = models.PositiveIntegerField()
    redaction_count = models.PositiveIntegerField(default=0)
    uploaded_at = models.DateTimeField(auto_now_add=True)

    objects = UploadedFileQuerySet.as_manager()

    class Meta:
        db_table = "diagnostics_uploaded_file"
        ordering = ["uploaded_at"]
        indexes = [
            models.Index(fields=["debug_session", "uploaded_at"]),
        ]
        constraints = [
            # Reject re-uploading identical content within the same session.
            models.UniqueConstraint(
                fields=["debug_session", "content_sha256"],
                name="uniq_upload_content_per_session",
            ),
        ]

    def __str__(self) -> str:
        return self.filename

    @property
    def owner_id(self):
        return self.debug_session.project.user_id


# ---------------------------------------------------------------------------
# DetectedIssue
# ---------------------------------------------------------------------------


class DetectedIssueQuerySet(models.QuerySet):
    def for_user(self, user) -> DetectedIssueQuerySet:
        return self.filter(debug_session__project__user=user)

    def by_priority(self) -> DetectedIssueQuerySet:
        """Order worst-first: severity rank, then confidence hint."""
        ranking = models.Case(
            *(models.When(severity=value, then=rank) for value, rank in _SEVERITY_RANK.items()),
            default=99,
            output_field=models.IntegerField(),
        )
        return self.alias(_severity_rank=ranking).order_by("_severity_rank", "-confidence_hint")


class DetectedIssue(UUIDModel):
    """A deterministic rule match with supporting evidence snippets."""

    debug_session = models.ForeignKey(
        DebugSession,
        on_delete=models.CASCADE,
        related_name="detected_issues",
    )
    issue_type = models.CharField(max_length=100)
    severity = models.CharField(max_length=20, choices=Severity.choices)
    confidence_hint = models.FloatField()
    matched_pattern = models.CharField(max_length=255, blank=True)
    evidence = models.JSONField(default=list)
    created_at = models.DateTimeField(auto_now_add=True)

    objects = DetectedIssueQuerySet.as_manager()

    class Meta:
        db_table = "diagnostics_detected_issue"
        ordering = ["-confidence_hint"]
        indexes = [
            models.Index(fields=["debug_session"]),
            models.Index(fields=["issue_type"]),
            models.Index(fields=["severity"]),
        ]
        constraints = [
            models.CheckConstraint(
                check=Q(confidence_hint__gte=0) & Q(confidence_hint__lt=1),
                name="detected_issue_confidence_range",
            ),
            models.CheckConstraint(
                check=Q(severity__in=Severity.values),
                name="detected_issue_severity_valid",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.issue_type} ({self.severity})"

    @property
    def owner_id(self):
        return self.debug_session.project.user_id


# ---------------------------------------------------------------------------
# DiagnosisReport
# ---------------------------------------------------------------------------


class DiagnosisReportQuerySet(models.QuerySet):
    def for_user(self, user) -> DiagnosisReportQuerySet:
        return self.filter(debug_session__project__user=user)


class DiagnosisReport(UUIDModel):
    """The schema-validated AI root-cause report (one per session).

    Semi-structured arrays are stored as JSONB because they are displayed as
    documents; core relations stay relational. Confidence is constrained below
    1.0 — the product never claims certainty.
    """

    debug_session = models.OneToOneField(
        DebugSession,
        on_delete=models.CASCADE,
        related_name="report",
    )
    root_cause = models.TextField()
    confidence_score = models.FloatField()
    severity = models.CharField(max_length=20, choices=Severity.choices)
    detected_stack = models.JSONField(default=list)
    detected_cloud_provider = models.CharField(max_length=100, blank=True)
    explanation = models.TextField()
    evidence_json = models.JSONField(default=list)
    recommended_fix = models.TextField()
    commands_json = models.JSONField(default=list)
    verification_checklist_json = models.JSONField(default=list)
    missing_information_json = models.JSONField(default=list)
    possible_risks_json = models.JSONField(default=list)
    model_name = models.CharField(max_length=100, blank=True)
    prompt_tokens = models.PositiveIntegerField(null=True, blank=True)
    completion_tokens = models.PositiveIntegerField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    objects = DiagnosisReportQuerySet.as_manager()

    class Meta:
        db_table = "diagnostics_diagnosis_report"
        ordering = ["-created_at"]
        constraints = [
            models.CheckConstraint(
                check=Q(confidence_score__gte=0) & Q(confidence_score__lt=1),
                name="report_confidence_range",
            ),
            models.CheckConstraint(
                check=Q(severity__in=Severity.values),
                name="report_severity_valid",
            ),
        ]

    def __str__(self) -> str:
        return f"Report for session {self.debug_session_id}"

    @property
    def owner_id(self):
        return self.debug_session.project.user_id
