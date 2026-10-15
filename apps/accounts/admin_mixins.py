"""Admin helpers shared by the user and organization admins."""

from __future__ import annotations

from typing import Any

from django.contrib import messages
from django.core.exceptions import ValidationError

from apps.organizations.models import OrganizationMembership

from . import services


class MembershipFormsetServiceMixin:
    """Save membership inlines through the identity services.

    A plain inline would write rows directly: no lifecycle rules, no ``left_at``, no
    ``organization_membership.*`` audit events.  Here every create / status change /
    default change is a service call, so the admin is held to the same rules as the app.
    The parent is either a ``User`` (organization chosen per row) or an ``Organization``
    (user chosen per row).
    """

    def save_formset(self, request, form, formset, change) -> None:  # noqa: ANN001
        if formset.model is not OrganizationMembership:
            return super().save_formset(request, form, formset, change)  # type: ignore[misc]
        parent = formset.instance
        # The admin's change message reads these (normally filled in by ``formset.save()``).
        formset.new_objects, formset.changed_objects, formset.deleted_objects = [], [], []
        for row in formset.forms:
            if not row.has_changed() or not getattr(row, "cleaned_data", None):
                continue
            data = row.cleaned_data
            user = parent if parent._meta.model_name == "user" else data.get("user")
            organization = parent if parent._meta.model_name == "organization" else data.get("organization")
            if user is None or organization is None:
                continue
            try:
                was_new = row.instance._state.adding  # UUID pks exist before saving
                saved = self._apply_membership_row(request, row, user, organization, data)
                if was_new:
                    formset.new_objects.append(saved)
                else:
                    formset.changed_objects.append((saved, row.changed_data))
            except ValidationError as exc:
                for message in exc.messages:
                    messages.error(request, message)

    @staticmethod
    def _apply_membership_row(request: Any, row: Any, user: Any, organization: Any, data: dict) -> Any:
        status = data.get("status") or OrganizationMembership.Status.ACTIVE
        wants_default = bool(data.get("is_default"))
        if row.instance._state.adding:
            membership = services.add_membership(
                user, organization, actor=request.user, is_default=wants_default and status == "active"
            )
            if membership.status != status:
                membership = services.set_membership_status(membership, status, actor=request.user)
            return membership
        membership = row.instance
        if "status" in row.changed_data:
            membership = services.set_membership_status(membership, status, actor=request.user)
        if "is_default" in row.changed_data and wants_default and membership.is_active:
            membership = services.set_default_organization(user, organization, actor=request.user)
        return membership
