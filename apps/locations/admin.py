"""Locations admin.

The admin forms enforce organization isolation: the parent location and the
location type choices are limited to the selected organization (plus
platform-defined types), so an editor cannot accidentally create a
cross-organization relationship.
"""

from __future__ import annotations

from django import forms
from django.contrib import admin
from simple_history.admin import SimpleHistoryAdmin

from .models import Location, LocationType
from .selectors import available_location_types


class LocationAdminForm(forms.ModelForm):
    """Admin form that scopes parent/type choices to the organization."""

    class Meta:
        model = Location
        fields = "__all__"

    def __init__(self, *args, **kwargs) -> None:  # noqa: ANN002, ANN003
        super().__init__(*args, **kwargs)
        organization = self.instance.organization if self.instance.pk else self.data.get("organization")
        if organization:
            self.fields["parent"].queryset = Location.objects.filter(
                organization_id=organization
            ).order_by("name")
            if self.instance.pk:
                self.fields["parent"].queryset = self.fields["parent"].queryset.exclude(
                    pk=self.instance.pk
                )
            self.fields["location_type"].queryset = available_location_types(organization)


@admin.register(LocationType)
class LocationTypeAdmin(SimpleHistoryAdmin):
    list_display = ("name", "code", "organization", "sort_order", "is_active")
    list_filter = ("is_active", "organization")
    search_fields = ("name", "code")
    ordering = ("organization", "sort_order", "name")
    readonly_fields = ("id", "created_at", "updated_at")
    list_per_page = 50


@admin.register(Location)
class LocationAdmin(SimpleHistoryAdmin):
    form = LocationAdminForm
    list_display = ("name", "code", "organization", "location_type", "parent", "is_active")
    list_filter = ("is_active", "organization", "location_type", "country")
    search_fields = ("name", "code", "city")
    autocomplete_fields = ("organization", "parent", "location_type")
    readonly_fields = ("id", "created_at", "updated_at")
    list_select_related = ("organization", "location_type", "parent")
    list_per_page = 50

    fieldsets = (
        (None, {"fields": ("id", "organization", "parent", "location_type", "name", "code", "is_active", "description")}),
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
        ("Advanced", {"fields": ("metadata",)}),
        ("Timestamps", {"fields": ("created_at", "updated_at")}),
    )
