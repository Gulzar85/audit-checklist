"""Locations admin.

The admin forms enforce organization isolation: once the organization is known the
parent location and location-type choices are limited to it (plus platform-defined
types), and a parent that would create a cycle is not offered.  The model's
``clean()`` and the database triggers re-check every rule, so a hand-crafted POST
cannot create a cross-organization link either.

Locations are deactivated, not deleted: deletion is disabled in the admin (and the
foreign keys are ``PROTECT`` regardless).
"""

from __future__ import annotations

from django import forms
from django.contrib import admin
from import_export.admin import ImportExportModelAdmin
from simple_history.admin import SimpleHistoryAdmin

from apps.core.admin import ActivityLogAdminMixin
from apps.core.utils import parse_uuid

from .models import Location, LocationType
from .resources import LocationResource, LocationTypeResource
from .selectors import get_descendants


class LocationAdminForm(forms.ModelForm):
    """Scope parent / type choices to the organization whenever it is known."""

    class Meta:
        model = Location
        fields = "__all__"

    def _organization_id(self):  # noqa: ANN202
        if self.instance.pk and not self.instance._state.adding:
            return self.instance.organization_id
        raw = self.data.get("organization") if self.is_bound else self.initial.get("organization")
        return parse_uuid(getattr(raw, "pk", raw))

    def __init__(self, *args, **kwargs) -> None:  # noqa: ANN002, ANN003
        super().__init__(*args, **kwargs)
        organization_id = self._organization_id()
        types = LocationType.objects.select_related("organization").order_by(
            "organization__name", "sort_order", "name"
        )
        parents = Location.objects.all()
        if organization_id:
            parents = parents.filter(organization_id=organization_id)
            types = types.available_to(organization_id)
        if self.instance.pk and not self.instance._state.adding:
            parents = parents.exclude(pk__in=get_descendants(self.instance, include_self=True))
        self.fields["parent"].queryset = parents
        self.fields["location_type"].queryset = types


@admin.register(LocationType)
class LocationTypeAdmin(ActivityLogAdminMixin, ImportExportModelAdmin, SimpleHistoryAdmin):
    resource_classes = [LocationTypeResource]
    activity_prefix = "location_type"

    list_display = ("name", "code", "organization", "sort_order", "is_active")
    list_filter = ("is_active", "organization")
    search_fields = ("name", "code", "organization__name", "organization__code")
    ordering = ("organization__name", "sort_order", "name")
    autocomplete_fields = ("organization",)
    readonly_fields = ("id", "created_at", "updated_at")
    list_select_related = ("organization",)
    list_per_page = 50

    fieldsets = (
        (None, {"fields": ("id", "organization", "name", "code", "description")}),
        ("Display", {"fields": ("sort_order", "is_active")}),
        ("Timestamps", {"fields": ("created_at", "updated_at")}),
    )

    def extra_activity(self, request, obj, form, change):  # noqa: ANN001, ANN201
        if change and "is_active" in form.changed_data:
            return [("location_type.activated" if obj.is_active else "location_type.deactivated", {})]
        return []


@admin.register(Location)
class LocationAdmin(ActivityLogAdminMixin, ImportExportModelAdmin, SimpleHistoryAdmin):
    resource_classes = [LocationResource]
    activity_prefix = "location"
    form = LocationAdminForm

    list_display = ("name", "code", "organization", "location_type", "parent", "is_active", "city", "country")
    list_filter = ("is_active", "organization", "location_type", "country")
    search_fields = ("name", "code", "city", "organization__name", "organization__code")
    ordering = ("organization__name", "name")
    autocomplete_fields = ("organization", "parent")
    readonly_fields = ("id", "created_at", "updated_at")
    list_select_related = ("organization", "location_type", "parent")
    list_per_page = 50

    fieldsets = (
        (
            None,
            {"fields": ("id", "organization", "location_type", "parent", "name", "code", "is_active", "description")},
        ),
        (
            "Address",
            {
                "fields": (
                    "address_line1",
                    "address_line2",
                    "city",
                    "state",
                    "postal_code",
                    "country",
                    "latitude",
                    "longitude",
                )
            },
        ),
        ("Regional", {"fields": ("timezone",)}),
        ("Advanced", {"fields": ("metadata",), "classes": ("collapse",)}),
        ("Timestamps", {"fields": ("created_at", "updated_at")}),
    )

    def get_readonly_fields(self, request, obj=None):  # noqa: ANN001, ANN201
        # A location never changes organization once it exists.
        extra = ("organization",) if obj is not None else ()
        return (*super().get_readonly_fields(request, obj), *extra)

    def has_delete_permission(self, request, obj=None) -> bool:  # noqa: ANN001
        return False

    def extra_activity(self, request, obj, form, change):  # noqa: ANN001, ANN201
        events: list[tuple[str, dict]] = []
        if change and "is_active" in form.changed_data:
            events.append(("location.activated" if obj.is_active else "location.deactivated", {}))
        if change and "parent" in form.changed_data:
            events.append(("location.parent_changed", {"to_parent": str(obj.parent_id) if obj.parent_id else None}))
        return events
