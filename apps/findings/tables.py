"""django-tables2 table for the findings list (badges use semantic tokens through the ``ui`` tags)."""

from __future__ import annotations

import django_tables2 as tables

from .models import Finding

_SEVERITY = "{% load ui %}{% tone_badge record.severity.name record.severity.tone icon='triangle-alert' %}"
_STATUS = "{% load ui %}{% status_badge record.effective_status %}"
_DUE = ("{% load ui %}{% if record.due_at %}{{ record.due_at|date:'M d, Y H:i' }}"
        "{% if record.is_overdue %} {% tone_badge 'Overdue' 'danger' icon='alarm-clock' %}{% endif %}{% else %}—{% endif %}")


class FindingTable(tables.Table):
    reference = tables.TemplateColumn(template_code="<a class='font-mono font-medium' href='{{ record.get_absolute_url }}'>{{ record.reference }}</a>",
                                      order_by=("number",), verbose_name="Reference")
    title = tables.TemplateColumn(
        template_code="<a href='{{ record.get_absolute_url }}'>{{ record.title }}</a>{% if record.needs_review %} <span class='text-xs text-warning-text'>needs review</span>{% endif %}",
        order_by=("title",), verbose_name="Finding")
    category = tables.Column(accessor="category__name", verbose_name="Category", order_by=("category__name",))
    severity = tables.TemplateColumn(template_code=_SEVERITY, order_by=("-severity__rank",), verbose_name="Severity")
    status = tables.TemplateColumn(template_code=_STATUS, order_by=("status",))
    location = tables.Column(accessor="location__name", verbose_name="Location", order_by=("location__name",))
    source_type = tables.Column(accessor="source_type", verbose_name="Source")
    assigned_to = tables.TemplateColumn(
        template_code="{% if record.assigned_to %}{{ record.assigned_to.get_short_name }}{% else %}<span class='text-muted-foreground'>Unassigned</span>{% endif %}",
        order_by=("assigned_to__email",), verbose_name="Responsible")
    detected_at = tables.DateTimeColumn(verbose_name="Detected", format="M d, Y")
    due_at = tables.TemplateColumn(template_code=_DUE, order_by=("due_at",), verbose_name="Due")

    class Meta:
        model = Finding
        fields = ("reference", "title", "category", "severity", "status", "location", "source_type", "assigned_to", "detected_at", "due_at")
        template_name = "core/tables/table.html"
        attrs = {"class": "table table-stack", "aria-label": "Findings"}
        empty_text = "No findings match your filters."
        order_by = ("-detected_at",)

    def render_source_type(self, record):  # noqa: ANN001, ANN201
        return record.get_source_type_display()
