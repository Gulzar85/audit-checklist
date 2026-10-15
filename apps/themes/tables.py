"""django-tables2 table for themes."""

from __future__ import annotations

import django_tables2 as tables

from .models import Theme


class ThemeTable(tables.Table):
    name = tables.Column(linkify=True, attrs={"a": {"class": "font-medium"}})
    status = tables.TemplateColumn(
        template_code='{% load ui %}{% status_badge record.status %}',
        order_by=("status",),
    )
    organization = tables.Column(default="System")
    version = tables.Column(verbose_name="Ver.")
    actions = tables.TemplateColumn(
        template_name="themes/_row_actions.html",
        orderable=False,
        verbose_name="Actions",
        attrs={"td": {"class": "text-right"}, "th": {"class": "text-right"}},
    )

    class Meta:
        model = Theme
        fields = ("name", "code", "scope", "organization", "version", "status", "default_appearance", "updated_at")
        template_name = "core/tables/table.html"
        attrs = {"class": "table table-stack", "aria-label": "Themes"}
        empty_text = "No themes match your filters."
