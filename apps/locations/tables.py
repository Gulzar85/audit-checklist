"""django-tables2 tables for the location domain.

Row-action visibility uses Django's ``perms`` as a *display hint*; every action is
re-authorised by the view through the policy layer when it is requested.
"""

from __future__ import annotations

import django_tables2 as tables

from .models import Location, LocationType

_ACTIVE_BADGE = '{% include "components/active_badge.html" with is_active=record.is_active %}'


class LocationTypeTable(tables.Table):
    """Paginated location-type list."""

    name = tables.Column(linkify=True, attrs={"a": {"class": "font-medium"}})
    scope = tables.Column(
        empty_values=(),
        accessor="organization__name",
        order_by=("organization__name",),
        verbose_name="Scope",
    )
    is_active = tables.TemplateColumn(
        template_code=_ACTIVE_BADGE, order_by=("is_active",), verbose_name="Status"
    )
    actions = tables.TemplateColumn(
        template_name="locations/_type_row_actions.html",
        orderable=False,
        verbose_name="Actions",
        attrs={"td": {"class": "text-right"}, "th": {"class": "text-right"}},
    )

    class Meta:
        model = LocationType
        fields = ("name", "code", "scope", "sort_order", "is_active")
        template_name = "core/tables/table.html"
        attrs = {"class": "table table-stack", "aria-label": "Location types"}
        empty_text = "No location types match your filters."

    def render_scope(self, record: LocationType) -> str:
        return record.organization.name if record.organization_id else "Platform-wide"


class LocationTable(tables.Table):
    """Paginated location list."""

    name = tables.Column(linkify=True, attrs={"a": {"class": "font-medium"}})
    organization = tables.Column(order_by=("organization__name",))
    location_type = tables.Column(
        accessor="location_type__name", order_by=("location_type__name",), verbose_name="Type"
    )
    parent = tables.Column(order_by=("parent__name",), default="—")
    is_active = tables.TemplateColumn(
        template_code=_ACTIVE_BADGE, order_by=("is_active",), verbose_name="Status"
    )
    actions = tables.TemplateColumn(
        template_name="locations/_row_actions.html",
        orderable=False,
        verbose_name="Actions",
        attrs={"td": {"class": "text-right"}, "th": {"class": "text-right"}},
    )

    class Meta:
        model = Location
        fields = ("name", "code", "organization", "location_type", "parent", "is_active", "city", "country")
        template_name = "core/tables/table.html"
        attrs = {"class": "table table-stack", "aria-label": "Locations"}
        empty_text = "No locations match your filters."
