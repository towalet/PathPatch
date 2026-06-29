"""
Object-level ownership permissions (defense in depth).

Querysets are the primary tenant boundary: every view scopes through
``Model.objects.for_user(request.user)``, so a foreign object is simply *not
found* (404, which also avoids leaking existence). These permission classes are
the object-level backstop required by docs/AGENT_PLAN.md §9.

Every diagnostics model exposes an ``owner_id`` property that resolves the owning
user through the Project.user chain, so the check is a single attribute compare.
"""

from __future__ import annotations

from rest_framework import permissions


class IsOwner(permissions.BasePermission):
    """Grant access only when the object resolves to ``request.user``."""

    message = "You do not have permission to access this resource."

    def has_object_permission(self, request, view, obj) -> bool:
        owner_id = getattr(obj, "owner_id", None)
        return owner_id is not None and owner_id == request.user.id
