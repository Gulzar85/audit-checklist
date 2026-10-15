"""django-tables2 table for themes."""

from __future__ import annotations

import django_tables2 as tables

from .models import Theme


class ThemeTable(tables.Table):
    name = tables.Column(linkify=True, attrs={"a": {"class": "font-medium"}})
    status = tables.TemplateColumn(
        template_code='{% include "themes/_status_badge.html" with status=record.status %}',
        order_by=("status",),
    )
    organization = tables.Column(default="System")
    version = tables.Column(verbose_name="Ver.")

    class Meta:
        model = Theme
        fields = ("name", "code", "scope", "organization", "version", "status", "default_appearance", "updated_at")
        template_name = "core/tables/table.html"
        attrs = {"class": "table", "aria-label": "Themes"}
        empty_text = "No themes match your filters."
