"""Forms for the location domain.

Choices are scoped to the selected organization so the UI never *offers* a
cross-organization option.  That is a convenience: the model, the service layer
and PostgreSQL each re-validate the relationship, so a tampered POST fails safely.
"""

from __future__ import annotations

from django import forms
from django.db.models import Q
from django.urls import reverse

from apps.core.forms import DatalistInput, DesignSystemFormMixin, SectionedFormMixin
from apps.core.utils import parse_uuid
from apps.organizations.models import Organization
from apps.organizations.selectors import organizations_visible_to

from .models import Location, LocationType
from apps.permissions.authorization import get_accessible_locations
from apps.permissions.registry import P

from .policies import can_create_location, can_create_location_in_organization, can_create_location_type
from .selectors import get_descendants
from .widgets import LocationPickerWidget


def _timezone_names() -> list[str]:
    from zoneinfo import available_timezones

    return sorted(available_timezones())


def _assignable_organizations(user):  # noqa: ANN001, ANN202
    """Organizations ``user`` may work in (active ones; staff may see inactive too)."""
    return organizations_visible_to(user).filter(status=Organization.Status.ACTIVE).order_by("name")


class LocationTypeForm(DesignSystemFormMixin, forms.ModelForm):
    """Create / edit a location type."""

    class Meta:
        model = LocationType
        fields = ("organization", "name", "code", "description", "sort_order")
        widgets = {"description": forms.TextInput(attrs={"placeholder": "Optional"})}
        help_texts = {
            "code": "Short unique code within the organization, e.g. REGION.",
            "sort_order": "Lower numbers are listed first.",
        }

    def __init__(self, *args, user=None, **kwargs) -> None:  # noqa: ANN002, ANN003
        super().__init__(*args, **kwargs)
        self.user = user
        organization = self.fields["organization"]
        organization.queryset = _assignable_organizations(user)
        organization.empty_label = "Platform-wide (all organizations)"
        organization.required = False
        organization.help_text = (
            "Leave as platform-wide for a type every organization can use "
            "(platform staff only)."
        )
        if self.instance.pk and not self.instance._state.adding:
            # A type never changes organization (and so never changes scope).
            # A disabled <select> still renders every option, so it may offer only the
            # type's own organization (nothing for a platform type) - never other tenants.
            organization.disabled = True
            organization.queryset = Organization.objects.filter(pk=self.instance.organization_id)

    def clean(self) -> dict:
        cleaned = super().clean()
        if not self.instance._state.adding or "organization" not in cleaned:
            return cleaned
        if not can_create_location_type(self.user, cleaned["organization"]):
            self.add_error(
                "organization",
                "You do not have permission to create location types here.",
            )
        return cleaned


class LocationForm(SectionedFormMixin, DesignSystemFormMixin, forms.ModelForm):
    """Create / edit a location."""

    address_fields = (
        "address_line1",
        "address_line2",
        "city",
        "state",
        "postal_code",
        "country",
        "latitude",
        "longitude",
        "timezone",
    )
    sections = (
        ("Basics", ("organization", "location_type", "parent", "name", "code", "description")),
        ("Address", address_fields),
    )

    class Meta:
        model = Location
        fields = (
            "organization",
            "location_type",
            "parent",
            "name",
            "code",
            "description",
            "address_line1",
            "address_line2",
            "city",
            "state",
            "postal_code",
            "country",
            "latitude",
            "longitude",
            "timezone",
        )
        labels = {
            "address_line1": "Address line 1",
            "address_line2": "Address line 2",
            "state": "State / province",
            "country": "Country code",
        }
        widgets = {
            "description": forms.Textarea(attrs={"rows": 3}),
            "address_line1": forms.TextInput(attrs={"autocomplete": "address-line1"}),
            "address_line2": forms.TextInput(attrs={"autocomplete": "address-line2"}),
            "city": forms.TextInput(attrs={"autocomplete": "address-level2"}),
            "state": forms.TextInput(attrs={"autocomplete": "address-level1"}),
            "postal_code": forms.TextInput(attrs={"autocomplete": "postal-code"}),
            "country": forms.TextInput(attrs={"autocomplete": "country", "maxlength": 2, "placeholder": "GB"}),
            "latitude": forms.NumberInput(attrs={"step": "any", "placeholder": "e.g. 51.5074"}),
            "longitude": forms.NumberInput(attrs={"step": "any", "placeholder": "e.g. -0.1278"}),
            "timezone": DatalistInput(_timezone_names, attrs={"placeholder": "Inherits the organization's"}),
            "parent": LocationPickerWidget(
                organization_source="[name=organization]", require_organization=True
            ),
        }
        help_texts = {
            "code": "Unique within the organization, e.g. KHI-001.",
            "timezone": "Leave empty to inherit the organization's time zone.",
        }

    def __init__(self, *args, user=None, organization=None, **kwargs) -> None:  # noqa: ANN002, ANN003
        super().__init__(*args, **kwargs)
        self.user = user
        editing = bool(self.instance.pk) and not self.instance._state.adding

        org_field = self.fields["organization"]
        org_field.queryset = _assignable_organizations(user)
        if editing:
            # A location never changes organization; keep the field fixed.
            org_field.disabled = True
            org_field.queryset = Organization.objects.filter(pk=self.instance.organization_id)
        else:
            # Re-scope the type and parent choices (HTMX) when the organization changes.
            org_field.widget.attrs.update(
                {
                    "hx-get": reverse("locations:form_relations"),
                    "hx-trigger": "change",
                    "hx-target": "#relations",
                    "hx-swap": "innerHTML",
                    "hx-include": "this",
                }
            )

        resolved = self._resolve_organization(organization, editing)
        self.organization = resolved
        self._scope_relations(resolved, editing)

    def _resolve_organization(self, explicit, editing: bool):  # noqa: ANN001, ANN202
        if explicit is not None:
            return explicit
        if editing:
            return self.instance.organization
        raw = self.data.get("organization") if self.is_bound else self.initial.get("organization")
        if isinstance(raw, Organization):
            return raw
        pk = parse_uuid(raw)
        if pk is None:
            return None
        return self.fields["organization"].queryset.filter(pk=pk).first()

    def _scope_relations(self, organization, editing: bool) -> None:  # noqa: ANN001
        parent_field = self.fields["parent"]
        type_field = self.fields["location_type"]
        if organization is None:
            parent_field.queryset = Location.objects.none()
            type_field.queryset = LocationType.objects.none()
            return

        # Only active locations are valid parents - but keep the *current* parent
        # selectable so editing an (inactive) location under an inactive parent works.
        current_parent = self.instance.parent_id if editing else None
        # RBAC: only locations the user may create under (or, when editing, move to) are offered.
        reachable = get_accessible_locations(
            self.user, P.LOCATION_MOVE if editing else P.LOCATION_CREATE, organization=organization
        )
        parents = Location.objects.filter(organization=organization).filter(
            Q(pk__in=reachable.values("pk")) | Q(pk=current_parent)
        ).filter(Q(is_active=True) | Q(pk=current_parent))
        if editing:
            parents = parents.exclude(pk__in=get_descendants(self.instance, include_self=True))
            parent_field.widget.exclude_id = self.instance.pk
        parent_field.queryset = parents
        if current_parent and not get_accessible_locations(
            self.user, P.LOCATION_VIEW, organization=organization
        ).filter(pk=current_parent).exists():
            # The parent lies above this user's scope: keep it, but neither show nor allow changing it.
            parent_field.disabled = True
            parent_field.widget.hide_label = "Above your access"
        parent_field.widget.organization_id = organization.pk if editing else None

        types = LocationType.objects.available_to(organization)
        current = self.instance.location_type_id if editing else None
        type_field.queryset = types.filter(Q(is_active=True) | Q(pk=current)).order_by("sort_order", "name")

    def clean_organization(self):  # noqa: ANN201
        organization = self.cleaned_data.get("organization")
        if organization is not None and not self.instance._state.adding:
            return organization
        if organization is not None and not can_create_location_in_organization(self.user, organization):
            raise forms.ValidationError("You do not have permission to create locations here.")
        return organization

    def clean(self) -> dict:
        cleaned = super().clean()
        organization, parent = cleaned.get("organization"), cleaned.get("parent")
        if (
            self.instance._state.adding
            and organization is not None
            and not self.has_error("organization")
            and not self.has_error("parent")
            and not can_create_location(self.user, organization, parent)
        ):
            self.add_error(
                "parent",
                "Choose a parent location you may add to."
                if parent is None
                else "You do not have permission to create locations here.",
            )
        return cleaned


class LocationStatusForm(forms.Form):
    """Validates an activate / deactivate request."""

    action = forms.ChoiceField(choices=(("activate", "Activate"), ("deactivate", "Deactivate")))
    cascade = forms.BooleanField(required=False)


class LocationTypeStatusForm(forms.Form):
    action = forms.ChoiceField(choices=(("activate", "Activate"), ("deactivate", "Deactivate")))
