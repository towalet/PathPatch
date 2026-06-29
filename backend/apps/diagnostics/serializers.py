"""
DRF serializers for the diagnostics domain.

Read/write are split where the contracts differ: clients write a small set of
fields (project name/metadata, a session's error summary) while reads expose
computed counts, nested evidence, and report context. Summary serializers keep
list/dashboard payloads lean; detail serializers carry the full document.
"""

from __future__ import annotations

from django.core.exceptions import ObjectDoesNotExist
from rest_framework import serializers

from .models import (
    DebugSession,
    DetectedIssue,
    DiagnosisReport,
    Project,
    UploadedFile,
)

# ---------------------------------------------------------------------------
# Project
# ---------------------------------------------------------------------------


class ProjectSerializer(serializers.ModelSerializer):
    """Read + write representation of a project.

    ``session_count`` / ``latest_session_at`` come from ``with_session_stats()``
    annotations on list/detail queries and fall back to "no sessions yet" for a
    freshly created instance.
    """

    session_count = serializers.SerializerMethodField()
    latest_session_at = serializers.SerializerMethodField()

    class Meta:
        model = Project
        fields = [
            "id",
            "name",
            "stack",
            "cloud_provider",
            "created_at",
            "updated_at",
            "session_count",
            "latest_session_at",
        ]
        read_only_fields = ["id", "created_at", "updated_at"]

    def get_session_count(self, obj) -> int:
        return getattr(obj, "session_count", 0)

    def get_latest_session_at(self, obj):
        return getattr(obj, "latest_session_at", None)

    def validate_name(self, value: str) -> str:
        name = value.strip()
        if not name:
            raise serializers.ValidationError("Project name cannot be blank.")
        user = self.context["request"].user
        clashes = Project.objects.for_user(user).filter(name__iexact=name)
        if self.instance is not None:
            clashes = clashes.exclude(pk=self.instance.pk)
        if clashes.exists():
            raise serializers.ValidationError("You already have a project with this name.")
        return name


# ---------------------------------------------------------------------------
# Uploaded files / detected issues
# ---------------------------------------------------------------------------


class UploadedFileSerializer(serializers.ModelSerializer):
    """File metadata only — the stored text content is never returned here."""

    class Meta:
        model = UploadedFile
        fields = [
            "id",
            "filename",
            "file_type",
            "size_bytes",
            "line_count",
            "redaction_count",
            "uploaded_at",
        ]
        read_only_fields = fields


class DetectedIssueSerializer(serializers.ModelSerializer):
    class Meta:
        model = DetectedIssue
        fields = [
            "id",
            "issue_type",
            "severity",
            "confidence_hint",
            "matched_pattern",
            "evidence",
            "created_at",
        ]
        read_only_fields = fields


# ---------------------------------------------------------------------------
# Reports
# ---------------------------------------------------------------------------


class DiagnosisReportSummarySerializer(serializers.ModelSerializer):
    """Lightweight report card for lists, dashboards, and session detail."""

    session_id = serializers.UUIDField(source="debug_session.id", read_only=True)
    project_id = serializers.UUIDField(source="debug_session.project.id", read_only=True)
    project_name = serializers.CharField(source="debug_session.project.name", read_only=True)

    class Meta:
        model = DiagnosisReport
        fields = [
            "id",
            "session_id",
            "project_id",
            "project_name",
            "root_cause",
            "confidence_score",
            "severity",
            "created_at",
        ]
        read_only_fields = fields


class DiagnosisReportSerializer(DiagnosisReportSummarySerializer):
    """The full report document with session/project context."""

    class Meta(DiagnosisReportSummarySerializer.Meta):
        fields = DiagnosisReportSummarySerializer.Meta.fields + [
            "detected_stack",
            "detected_cloud_provider",
            "explanation",
            "evidence_json",
            "recommended_fix",
            "commands_json",
            "verification_checklist_json",
            "missing_information_json",
            "possible_risks_json",
            "model_name",
            "prompt_tokens",
            "completion_tokens",
        ]
        read_only_fields = fields


# ---------------------------------------------------------------------------
# Debug sessions
# ---------------------------------------------------------------------------


class DebugSessionCreateSerializer(serializers.ModelSerializer):
    """Write contract for starting a session; project comes from the URL."""

    class Meta:
        model = DebugSession
        fields = ["error_summary"]


class DebugSessionSummarySerializer(serializers.ModelSerializer):
    """Session row for lists, history, and dashboard rails."""

    project_id = serializers.UUIDField(source="project.id", read_only=True)
    project_name = serializers.CharField(source="project.name", read_only=True)
    report_id = serializers.SerializerMethodField()

    class Meta:
        model = DebugSession
        fields = [
            "id",
            "project_id",
            "project_name",
            "status",
            "error_summary",
            "report_id",
            "created_at",
            "updated_at",
        ]
        read_only_fields = fields

    def get_report_id(self, obj):
        try:
            return obj.report.id
        except ObjectDoesNotExist:
            return None


class DebugSessionDetailSerializer(serializers.ModelSerializer):
    """Full session view: files, detected issues, and report summary."""

    project_id = serializers.UUIDField(source="project.id", read_only=True)
    project_name = serializers.CharField(source="project.name", read_only=True)
    files = UploadedFileSerializer(many=True, read_only=True)
    detected_issues = DetectedIssueSerializer(many=True, read_only=True)
    report = serializers.SerializerMethodField()

    class Meta:
        model = DebugSession
        fields = [
            "id",
            "project_id",
            "project_name",
            "status",
            "error_summary",
            "failure_reason",
            "analysis_started_at",
            "analysis_completed_at",
            "created_at",
            "updated_at",
            "files",
            "detected_issues",
            "report",
        ]
        read_only_fields = fields

    def get_report(self, obj):
        try:
            return DiagnosisReportSummarySerializer(obj.report).data
        except ObjectDoesNotExist:
            return None


class ProjectDetailSerializer(ProjectSerializer):
    """Project metadata plus its most recent sessions."""

    recent_sessions = serializers.SerializerMethodField()

    class Meta(ProjectSerializer.Meta):
        fields = ProjectSerializer.Meta.fields + ["recent_sessions"]

    def get_recent_sessions(self, obj):
        sessions = obj.sessions.select_related("project", "report")[:10]
        return DebugSessionSummarySerializer(sessions, many=True).data


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------


class DashboardSerializer(serializers.Serializer):
    """Aggregate counts and recent activity for the dashboard."""

    project_count = serializers.IntegerField()
    session_count = serializers.IntegerField()
    completed_session_count = serializers.IntegerField()
    failed_session_count = serializers.IntegerField()
    high_severity_count = serializers.IntegerField()
    recent_sessions = DebugSessionSummarySerializer(many=True)
    recent_reports = DiagnosisReportSummarySerializer(many=True)
