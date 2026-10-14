"""django-tables2 tables for organizations."""

from __future__ import annotations

import django_tables2 as tables

from .models import Organization


class OrganizationTable(tables.Table):
    """Paginated organization list."""

    name = tables.Column(linkify=True, attrs={"a": {"class": "font-medium"}})
    status = tables.TemplateColumn(
        template_code=(
            '{% include "components/status_badge.html" '
            'with label=record.get_status_display tone=record.status %}'
        ),
        order_by=("status",),
    )
    actions = tables.TemplateColumn(
        template_name="organizations/_row_actions.html",
        orderable=False,
        verbose_name="",
        attrs={"td": {"class": "text-right"}},
    )

    class Meta:
        model = Organization
        fields = ("name", "code", "status", "city", "country", "created_at")
        template_name = "core/tables/table.html"
        attrs = {"class": "table", "aria": {"label": "Organizations"}}
        empty_text = "No organizations match your search."
