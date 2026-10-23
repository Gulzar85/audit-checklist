"""django-tables2 table for the corrective-action lists (badges use semantic tokens through the ``ui`` tags)."""

from __future__ import annotations

import django_tables2 as tables

from .models import CorrectiveAction

_PRIORITY = ("{% load ui %}{% if record.priority == 'critical' %}{% tone_badge 'Critical' 'danger' icon='flame' %}{% elif record.priority == 'high' %}"
             "{% tone_badge 'High' 'warning' icon='arrow-up' %}{% elif record.priority == 'low' %}{% tone_badge 'Low' 'neutral' icon='arrow-down' %}"
             "{% else %}{% tone_badge 'Normal' 'info' icon='minus' %}{% endif %}")
_STATUS = "{% load ui %}{% status_badge record.effective_status %}"
_DUE = ("{% load ui %}{% if record.due_at %}{{ record.due_at|date:'M d, Y H:i' }}"
        "{% if record.is_overdue %} {% tone_badge 'Overdue' 'danger' icon='alarm-clock' %}{% endif %}{% else %}—{% endif %}")
_FINDING = ("{% load ui %}<a href='{{ record.finding.get_absolute_url }}' class='font-mono'>{{ record.finding.reference }}</a> "
            "{% tone_badge record.finding.severity.name record.finding.severity.tone icon='triangle-alert' %}")


class ActionTable(tables.Table):
    reference = tables.TemplateColumn(template_code="<a class='font-mono font-medium' href='{{ record.get_absolute_url }}'>{{ record.reference }}</a>",
                                      order_by=("number",), verbose_name="Reference")
    title = tables.TemplateColumn(template_code="<a href='{{ record.get_absolute_url }}'>{{ record.title }}</a>", order_by=("title",), verbose_name="Action")
    finding = tables.TemplateColumn(template_code=_FINDING, order_by=("finding__number",), verbose_name="Finding")
    location = tables.Column(accessor="location__name", verbose_name="Location", order_by=("location__name",))
    assigned_to = tables.TemplateColumn(
        template_code="{% if record.assigned_to %}{{ record.assigned_to.get_short_name }}{% else %}<span class='text-muted-foreground'>Unassigned</span>{% endif %}",
        order_by=("assigned_to__email",), verbose_name="Responsible")
    priority = tables.TemplateColumn(template_code=_PRIORITY, order_by=("priority_order",))
    status = tables.TemplateColumn(template_code=_STATUS, order_by=("status",))
    due_at = tables.TemplateColumn(template_code=_DUE, order_by=("due_at",), verbose_name="Due")
    updated_at = tables.DateTimeColumn(verbose_name="Updated", format="M d, Y H:i")

    class Meta:
        model = CorrectiveAction
        fields = ("reference", "title", "finding", "location", "assigned_to", "priority", "status", "due_at", "updated_at")
        template_name = "core/tables/table.html"
        attrs = {"class": "table table-stack", "aria-label": "Corrective actions"}
        empty_text = "No corrective actions match your filters."
        order_by = ("-created_at",)
