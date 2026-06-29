"""Django admin registrations for the diagnostics models.

Read-mostly admin: the analysis pipeline owns writes, so admin is tuned for
inspection (list filters, search, raw-id FKs, collapsed read-only timestamps)
rather than data entry.
"""

from __future__ import annotations

from django.contrib import admin

from .models import (
    DebugSession,
    DetectedIssue,
    DiagnosisReport,
    Project,
    UploadedFile,
)


@admin.register(Project)
class ProjectAdmin(admin.ModelAdmin):
    list_display = ("name", "user", "stack", "cloud_provider", "created_at")
    list_filter = ("cloud_provider", "created_at")
    search_fields = ("name", "user__email", "stack")
    raw_id_fields = ("user",)
    readonly_fields = ("id", "created_at", "updated_at")
    ordering = ("-created_at",)


@admin.register(DebugSession)
class DebugSessionAdmin(admin.ModelAdmin):
    list_display = ("id", "project", "status", "created_at", "analysis_completed_at")
    list_filter = ("status", "created_at")
    search_fields = ("id", "project__name", "error_summary")
    raw_id_fields = ("project",)
    readonly_fields = ("id", "created_at", "updated_at")
    ordering = ("-created_at",)


@admin.register(UploadedFile)
class UploadedFileAdmin(admin.ModelAdmin):
    list_display = (
        "filename",
        "file_type",
        "debug_session",
        "size_bytes",
        "redaction_count",
        "uploaded_at",
    )
    list_filter = ("file_type", "uploaded_at")
    search_fields = ("filename", "debug_session__id")
    raw_id_fields = ("debug_session",)
    readonly_fields = ("id", "content_sha256", "uploaded_at")


@admin.register(DetectedIssue)
class DetectedIssueAdmin(admin.ModelAdmin):
    list_display = ("issue_type", "severity", "confidence_hint", "debug_session", "created_at")
    list_filter = ("severity", "issue_type", "created_at")
    search_fields = ("issue_type", "matched_pattern", "debug_session__id")
    raw_id_fields = ("debug_session",)
    readonly_fields = ("id", "created_at")


@admin.register(DiagnosisReport)
class DiagnosisReportAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "debug_session",
        "severity",
        "confidence_score",
        "model_name",
        "created_at",
    )
    list_filter = ("severity", "created_at")
    search_fields = ("root_cause", "debug_session__id")
    raw_id_fields = ("debug_session",)
    readonly_fields = ("id", "created_at")
