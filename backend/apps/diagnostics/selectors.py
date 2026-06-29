"""
Read-side query helpers (the "selector" layer).

Views stay thin by delegating any non-trivial read — annotations, aggregations,
ownership-scoped fetches — to these functions. Every selector takes the acting
``user`` and scopes through the ownership chain, so callers cannot accidentally
read across tenants. Write-side business logic lives in ``services/`` instead.
"""

from __future__ import annotations

from django.db.models import Count, Q

from .models import (
    DebugSession,
    DiagnosisReport,
    Project,
    SessionStatus,
    Severity,
)

# How many items the dashboard surfaces in its "recent" rails.
RECENT_LIMIT = 5


def projects_for_user(user):
    """User's projects, annotated with session counts, newest first.

    Ordering is explicit (not just Meta.ordering) so paginated list responses
    stay deterministic even though the aggregate annotation adds a GROUP BY.
    """
    return Project.objects.for_user(user).with_session_stats().order_by("-created_at")


def project_sessions(user, project: Project):
    """Sessions for one owned project, newest first (project already scoped)."""
    return project.sessions.all()


def dashboard_summary(user) -> dict:
    """Aggregate counts plus recent sessions/reports for the dashboard.

    Counts are computed in two grouped queries rather than several ``count()``
    round-trips; recent rails use ``select_related`` to avoid N+1 on project.
    """
    session_counts = DebugSession.objects.for_user(user).aggregate(
        session_count=Count("id"),
        completed_session_count=Count("id", filter=Q(status=SessionStatus.COMPLETED)),
        failed_session_count=Count("id", filter=Q(status=SessionStatus.FAILED)),
    )
    high_severity_count = (
        DiagnosisReport.objects.for_user(user).filter(severity=Severity.HIGH).count()
    )

    recent_sessions = list(
        DebugSession.objects.for_user(user).select_related("project", "report")[:RECENT_LIMIT]
    )
    recent_reports = list(
        DiagnosisReport.objects.for_user(user).select_related("debug_session__project")[
            :RECENT_LIMIT
        ]
    )

    return {
        "project_count": Project.objects.for_user(user).count(),
        "session_count": session_counts["session_count"],
        "completed_session_count": session_counts["completed_session_count"],
        "failed_session_count": session_counts["failed_session_count"],
        "high_severity_count": high_severity_count,
        "recent_sessions": recent_sessions,
        "recent_reports": recent_reports,
    }
