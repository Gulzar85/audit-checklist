"""django-tables2 tables for the location domain."""

from __future__ import annotations

import django_tables2 as tables

from .models import Location, LocationType

_ACTIVE_BADGE = '{% include "components/active_badge.html" with is_active=record.is_active %}'


class LocationTypeTable(tables.Table):
    """Paginated location-type list."""

    name = tables.Column(linkify=True, attrs={"a": {"class": "font-medium"}})
    scope = tables.Column(empty_values=(), verbose_name="Scope")
    is_active = tables.TemplateColumn(template_code=_ACTIVE_BADGE, order_by="is_active")
    actions = tables.TemplateColumn(
        template_name="locations/_type_row_actions.html",
        orderable=False,
        verbose_name="",
        attrs={"td": {"class": "text-right"}},
    )

    class Meta:
        model = LocationType
        fields = ("name", "code", "scope", "sort_order", "is_active")
        template_name = "core/tables/table.html"
        attrs = {"class": "table"}
        empty_text = "No location types yet."

    def render_scope(self, record: LocationType) -> str:  # noqa: ANN001
        return record.organization.name if record.organization_id else "Platform"


class LocationTable(tables.Table):
    """Paginated location list."""

    name = tables.Column(linkify=True, attrs={"a": {"class": "font-medium"}})
    is_active = tables.TemplateColumn(template_code=_ACTIVE_BADGE, order_by="is_active")
    actions = tables.TemplateColumn(
        template_name="locations/_row_actions.html",
        orderable=False,
        verbose_name="",
        attrs={"td": {"class": "text-right"}},
    )

    class Meta:
        model = Location
        fields = (
            "name",
            "code",
            "organization",
            "location_type",
            "parent",
            "is_active",
            "city",
            "country",
        )
        template_name = "core/tables/table.html"
        attrs = {"class": "table", "aria": {"label": "Locations"}}
        empty_text = "No locations match your search."
