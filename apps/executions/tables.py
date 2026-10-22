"""django-tables2 tables for the worklist and the execution history (badges use semantic tokens through the ``ui`` tags)."""

from __future__ import annotations

import django_tables2 as tables

from .models import ChecklistExecution

_PRIORITY = "{% load ui %}{% if record.priority == 'critical' %}{% tone_badge 'Critical' 'danger' icon='flame' %}{% elif record.priority == 'high' %}{% tone_badge 'High' 'warning' icon='arrow-up' %}{% elif record.priority == 'low' %}{% tone_badge 'Low' 'neutral' icon='arrow-down' %}{% else %}{% tone_badge 'Normal' 'info' icon='minus' %}{% endif %}"


class MyWorkTable(tables.Table):
    checklist = tables.TemplateColumn(
        template_code="<span class='font-medium'>{{ record.version.template.name }}</span> <span class='text-xs text-muted-foreground'>v{{ record.version.version_number }}</span><br><span class='font-mono text-xs text-muted-foreground'>{{ record.reference }}</span>",
        order_by=("version__template__name",), verbose_name="Checklist",
    )
    location = tables.TemplateColumn(template_code="{{ record.path|default:record.location.name }}", order_by=("location__name",), verbose_name="Location")
    due_at = tables.TemplateColumn(
        template_code="{% if record.due_at %}{{ record.due_at|date:'M d, Y H:i' }}{% else %}<span class='text-muted-foreground'>No deadline</span>{% endif %}",
        verbose_name="Due", order_by=("due_at",),
    )
    priority = tables.TemplateColumn(template_code=_PRIORITY, order_by=("priority_rank",))
    status = tables.TemplateColumn(template_code="{% load ui %}{% status_badge record.effective_status %}", verbose_name="Assignment", order_by=("status",))
    progress = tables.TemplateColumn(
        template_code="{% if record.live %}<div class='flex items-center gap-2'><progress class='h-2 w-24' max='100' value='{{ record.progress|default:0 }}' aria-label='Progress'></progress><span class='text-xs'>{{ record.progress|default:0 }}%</span></div>{% else %}<span class='text-muted-foreground'>Not started</span>{% endif %}",
        orderable=False, verbose_name="Progress",
    )
    actions = tables.TemplateColumn(
        template_name="executions/_work_actions.html", orderable=False, verbose_name="Action",
        attrs={"td": {"class": "text-right"}, "th": {"class": "text-right"}},
    )

    class Meta:
        from apps.assignments.models import ChecklistAssignment

        model = ChecklistAssignment
        fields = ("checklist", "location", "due_at", "priority", "status", "progress")
        template_name = "core/tables/table.html"
        attrs = {"class": "table table-stack", "aria-label": "My checklist work"}
        empty_text = "No work matches your filters."


class ExecutionTable(tables.Table):
    reference = tables.TemplateColumn(
        template_code="<a class='font-mono font-medium' href='{{ record.get_absolute_url }}'>{{ record.reference }}</a>",
        order_by=("number",), verbose_name="Execution",
    )
    checklist = tables.TemplateColumn(
        template_code="{{ record.version.template.name }} <span class='text-xs text-muted-foreground'>v{{ record.version.version_number }}</span>",
        order_by=("version__template__name",), verbose_name="Checklist",
    )
    location = tables.Column(accessor="assignment__location__name", verbose_name="Location", order_by=("assignment__location__name",))
    status = tables.TemplateColumn(template_code="{% load ui %}{% status_badge record.status %}", order_by=("status",))
    started_by = tables.TemplateColumn(template_code="{% if record.started_by %}{{ record.started_by.get_short_name }}{% else %}—{% endif %}", verbose_name="Started by")
    started_at = tables.DateTimeColumn(format="M d, Y H:i", verbose_name="Started")
    submitted_at = tables.DateTimeColumn(format="M d, Y H:i", verbose_name="Submitted", default="—")
    actions = tables.TemplateColumn(
        template_code="{% load ui %}{% url 'executions:detail' record.pk as u %}<div class='flex justify-end'>{% row_action 'View' 'eye' href=u subject=record.reference %}</div>",
        orderable=False, verbose_name="Actions", attrs={"td": {"class": "text-right"}, "th": {"class": "text-right"}},
    )

    class Meta:
        model = ChecklistExecution
        fields = ("reference", "checklist", "location", "status", "started_by", "started_at", "submitted_at")
        template_name = "core/tables/table.html"
        attrs = {"class": "table table-stack", "aria-label": "Checklist executions"}
        empty_text = "No executions yet."
