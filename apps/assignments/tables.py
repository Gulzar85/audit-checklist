"""django-tables2 table for the assignment list (badges use semantic tokens through the ``ui`` tags)."""

from __future__ import annotations

import django_tables2 as tables

from .models import ChecklistAssignment

_PRIORITY_TONE = "{% load ui %}{% if record.priority == 'critical' %}{% tone_badge 'Critical' 'danger' icon='flame' %}{% elif record.priority == 'high' %}{% tone_badge 'High' 'warning' icon='arrow-up' %}{% elif record.priority == 'low' %}{% tone_badge 'Low' 'neutral' icon='arrow-down' %}{% else %}{% tone_badge 'Normal' 'info' icon='minus' %}{% endif %}"


class AssignmentTable(tables.Table):
    reference = tables.TemplateColumn(
        template_code="<a class='font-mono font-medium' href='{{ record.get_absolute_url }}'>{{ record.reference }}</a>",
        order_by=("number",), verbose_name="Reference",
    )
    checklist = tables.TemplateColumn(
        template_code="{{ record.version.template.name }} <span class='text-xs text-muted-foreground'>v{{ record.version.version_number }}</span>",
        order_by=("version__template__name",), verbose_name="Checklist",
    )
    location = tables.Column(accessor="location__name", verbose_name="Location", order_by=("location__name",))
    assigned_to = tables.TemplateColumn(
        template_code="{% if record.assigned_to %}{{ record.assigned_to.get_short_name }}{% else %}<span class='text-muted-foreground'>Location team</span>{% endif %}",
        order_by=("assigned_to__email",), verbose_name="Responsible",
    )
    issued_at = tables.DateTimeColumn(verbose_name="Assigned", format="M d, Y", default="—")
    due_at = tables.TemplateColumn(
        template_code="{% if record.due_at %}{{ record.due_at|date:'M d, Y H:i' }}{% else %}—{% endif %}", verbose_name="Due", order_by=("due_at",),
    )
    priority = tables.TemplateColumn(template_code=_PRIORITY_TONE, order_by=("priority_rank",))
    status = tables.TemplateColumn(template_code="{% load ui %}{% status_badge record.effective_status %}", order_by=("status",))
    created_by = tables.TemplateColumn(
        template_code="{% if record.created_by %}{{ record.created_by.get_short_name }}{% else %}—{% endif %}", verbose_name="Created by",
        order_by=("created_by__email",),
    )
    actions = tables.TemplateColumn(
        template_name="assignments/_row_actions.html", orderable=False, verbose_name="Actions",
        attrs={"td": {"class": "text-right"}, "th": {"class": "text-right"}},
    )

    class Meta:
        model = ChecklistAssignment
        fields = ("reference", "checklist", "location", "assigned_to", "issued_at", "due_at", "priority", "status", "created_by")
        template_name = "core/tables/table.html"
        attrs = {"class": "table table-stack", "aria-label": "Checklist assignments"}
        empty_text = "No assignments match your filters."
