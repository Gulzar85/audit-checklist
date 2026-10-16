"""django-tables2 table for the checklist template list (icon row actions; the views re-authorize)."""

from __future__ import annotations

import django_tables2 as tables

from .models import ChecklistTemplate


class TemplateTable(tables.Table):
    code = tables.Column(linkify=True, attrs={"a": {"class": "font-mono font-medium"}})
    name = tables.Column(linkify=True, attrs={"a": {"class": "font-medium"}})
    category = tables.Column(default="—", order_by=("category__name",))
    status = tables.TemplateColumn(template_code="{% load ui %}{% status_badge record.status %}", order_by=("status",))
    version = tables.TemplateColumn(
        template_code=(
            '{% if record.latest_version_number %}v{{ record.latest_version_number }}'
            '{% if record.published_version_number != record.latest_version_number %}'
            ' <span class="badge badge-warning">draft</span>{% endif %}{% else %}—{% endif %}'
        ),
        order_by=("latest_version_number",), verbose_name="Version",
    )
    actions = tables.TemplateColumn(
        template_name="checklists/_row_actions.html", orderable=False, verbose_name="Actions",
        attrs={"td": {"class": "text-right"}, "th": {"class": "text-right"}},
    )

    class Meta:
        model = ChecklistTemplate
        fields = ("code", "name", "category", "status", "version", "updated_at")
        template_name = "core/tables/table.html"
        attrs = {"class": "table table-stack", "aria-label": "Checklist templates"}
        empty_text = "No checklist templates match your filters."
