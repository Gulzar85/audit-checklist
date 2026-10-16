"""Forms for roles and role assignment.

Forms only *shape and validate input*.  Authorization is decided by the policies and enforced
again in the services, so a hand-crafted POST gains nothing the form hid.
"""

from __future__ import annotations

from typing import Any

from django import forms
from django.db.models import Q

from apps.core.forms import DesignSystemFormMixin
from apps.locations.widgets import LocationPickerWidget

from . import registry
from .authorization import get_accessible_locations
from .models import MembershipRole, Role, RolePermission
from .policies import assignable_codes, can_assign_role, role_permission_codes


class RoleForm(DesignSystemFormMixin, forms.Form):
    """Name, description and the permission set of a custom role.

    ``permissions`` is rendered as the permission matrix (not a flat list), so the widget is a
    plain ``MultipleChoiceField`` the template draws by hand.
    """

    name = forms.CharField(max_length=120)
    description = forms.CharField(max_length=255, required=False)
    permissions = forms.MultipleChoiceField(
        required=False,
        choices=[(definition.code, definition.label) for definition in registry.REGISTRY],
        widget=forms.CheckboxSelectMultiple,
    )

    def __init__(self, *args: Any, role: Role | None = None, actor: Any = None, organization: Any = None, **kwargs: Any) -> None:
        self.role = role
        self.actor = actor
        self.organization = organization
        if role is not None and "initial" not in kwargs:
            kwargs["initial"] = {
                "name": role.name,
                "description": role.description,
                "permissions": role_permission_codes(role),
            }
        super().__init__(*args, **kwargs)
        self.fields["description"].widget.attrs.setdefault("placeholder", "What is this role for?")

    def clean_permissions(self) -> list[str]:
        codes = self.cleaned_data["permissions"]
        if self.actor is not None and not getattr(self.actor, "is_superuser", False):
            held = assignable_codes(self.actor, self.organization)
            kept = set(role_permission_codes(self.role)) if self.role else set()
            extra = [code for code in codes if code not in held and code not in kept]
            if extra:  # escalation: granting what you do not hold
                raise forms.ValidationError("You cannot grant permissions you do not hold yourself.")
            removed_unheld = [code for code in kept if code not in held and code not in codes]
            if removed_unheld:
                raise forms.ValidationError("This role carries permissions you do not hold, so you cannot change them.")
        return codes


class AssignRoleForm(DesignSystemFormMixin, forms.Form):
    """Grant a role to a member within a scope."""

    role = forms.ModelChoiceField(queryset=Role.objects.none())
    scope_type = forms.ChoiceField(choices=MembershipRole.ScopeType.choices, initial=MembershipRole.ScopeType.ORGANIZATION)
    location = forms.ModelChoiceField(
        queryset=None, required=False, widget=LocationPickerWidget(), help_text="Required for a location scope."
    )

    def __init__(self, *args: Any, membership: Any, actor: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.membership = membership
        self.actor = actor
        self.denied = False
        organization_id = membership.organization_id
        # Only roles the actor could actually grant are offered (the policy re-checks on submit).
        roles = Role.objects.filter(
            Q(organization_id=organization_id) | Q(organization__isnull=True), is_active=True
        ).order_by("-is_system", "name")
        if not getattr(actor, "is_superuser", False):
            held = assignable_codes(actor, membership.organization)  # at its widest: a scoped grant narrows further
            by_role: dict = {}
            for role_id, code in RolePermission.objects.filter(role__in=roles, permission__is_active=True).values_list(
                "role_id", "permission__code"
            ):
                by_role.setdefault(role_id, set()).add(code)
            allowed = [r.pk for r in roles if by_role.get(r.pk, set()) <= held and r.slug != "super-administrator"]
            roles = roles.filter(pk__in=allowed)
        self.fields["role"].queryset = roles
        self.all_roles = Role.objects.filter(
            Q(organization_id=organization_id) | Q(organization__isnull=True), is_active=True
        )  # for telling "not offered to you" (403) apart from "does not exist" (form error)
        reachable = get_accessible_locations(actor, registry.P.LOCATION_VIEW, organization=organization_id).filter(
            is_active=True
        )
        self.fields["location"].queryset = reachable
        self.fields["location"].widget.organization_id = organization_id

    def clean(self) -> dict[str, Any]:
        cleaned = super().clean()
        role, scope_type, location = cleaned.get("role"), cleaned.get("scope_type"), cleaned.get("location")
        if role is None or scope_type is None:
            return cleaned
        if scope_type == MembershipRole.ScopeType.ORGANIZATION:
            location = None
            cleaned["location"] = None
        elif location is None:
            self.add_error("location", "Choose the location this role applies to.")
            return cleaned
        if not can_assign_role(self.actor, self.membership, role, scope_type=scope_type, location=location):
            self.denied = True
            raise forms.ValidationError("You do not have permission to grant this role at that scope.")
        return cleaned


class MembershipStatusForm(forms.Form):
    status = forms.ChoiceField(choices=[
        ("active", "Active"), ("suspended", "Suspended"), ("left", "Inactive"), ("pending", "Pending"),
    ])
