"""Core admin registrations and shared admin helpers."""

from __future__ import annotations

from typing import Any

from django.contrib import admin

from .models import ActivityLog
from .services import log_activity, stamp_history


class ActivityLogAdminMixin:
    """Record admin edits in the business activity log.

    Edits made in the admin bypass the service layer, so without this they would
    leave no business-level trail.  Each save is logged as ``<prefix>.created`` or
    ``<prefix>.updated`` with ``source=admin`` and the changed field names;
    subclasses may add more specific events via :meth:`extra_activity`.
    """

    activity_prefix = ""

    def activity_organization(self, obj: Any) -> Any:
        return getattr(obj, "organization", None)

    def guard_tenant_write(self, obj: Any) -> None:
        """Re-validate inside the organization lock, like the service layer does.

        Form validation ran before this point; a concurrent deactivation/suspension
        could have changed the world since.  Models owned by an organization (not the
        organization itself) therefore lock it, require it to be active and re-run
        ``full_clean`` before saving.
        """
        from apps.organizations.models import Organization

        if isinstance(obj, Organization) or not getattr(obj, "organization_id", None):
            return
        from apps.locations.services import lock_organization, require_active_organization

        require_active_organization(lock_organization(obj.organization_id))
        obj.full_clean()

    def extra_activity(self, request, obj: Any, form, change: bool) -> list[tuple[str, dict]]:  # noqa: ANN001
        """Return additional ``(verb, metadata)`` events for this save."""
        return []

    def save_model(self, request, obj, form, change) -> None:  # noqa: ANN001
        verb = f"{self.activity_prefix}.{'updated' if change else 'created'}"
        self.guard_tenant_write(obj)
        stamp_history(obj, request.user, verb)
        super().save_model(request, obj, form, change)  # type: ignore[misc]
        organization = self.activity_organization(obj)
        description = f"{'Updated' if change else 'Created'} {obj} (admin)"
        log_activity(
            verb=verb,
            organization=organization,
            actor=request.user,
            description=description,
            target=obj,
            metadata={"source": "admin", "changed_fields": sorted(form.changed_data)},
        )
        for extra_verb, metadata in self.extra_activity(request, obj, form, change):
            log_activity(
                verb=extra_verb,
                organization=organization,
                actor=request.user,
                description=description,
                target=obj,
                metadata={"source": "admin", **metadata},
            )


@admin.register(ActivityLog)
class ActivityLogAdmin(admin.ModelAdmin):
    list_display = ("created_at", "verb", "actor", "organization", "target_type", "target_id")
    list_filter = ("verb", "target_type")
    search_fields = ("verb", "description", "target_id")
    readonly_fields = (
        "id",
        "organization",
        "actor",
        "verb",
        "description",
        "target_type",
        "target_id",
        "metadata",
        "created_at",
    )
    date_hierarchy = "created_at"

    def has_add_permission(self, request) -> bool:  # noqa: ANN001
        return False
