"""Django admin for workflows: a *read-only* inspection tool.

There is deliberately no add / change / delete and no bulk action: a write here would skip validation, publication, immutability, assignee and separation-of-duties rules
and the business-action registry (definitions), or the engine itself (instances, tasks, events).  Published configuration is additionally frozen by database triggers, and events are
append-only.  Non-superusers only see what they may see through the application (definitions of their organizations; instances and tasks in their scope).
"""

from __future__ import annotations

from django.contrib import admin
from simple_history.admin import SimpleHistoryAdmin

from . import selectors
from .models import WorkflowDefinition, WorkflowEvent, WorkflowInstance, WorkflowState, WorkflowTask, WorkflowTransition, WorkflowVersion


class ReadOnlyMixin:
    actions = None

    def has_add_permission(self, request) -> bool:  # noqa: ANN001
        return False

    def has_change_permission(self, request, obj=None) -> bool:  # noqa: ANN001
        return False

    def has_delete_permission(self, request, obj=None) -> bool:  # noqa: ANN001
        return False


def _scope(request, qs, kind: str, through: str = ""):  # noqa: ANN001, ANN202
    if request.user.is_superuser:
        return qs
    visible = {"definitions": selectors.definitions_visible_to, "versions": selectors.versions_visible_to, "instances": selectors.instances_visible_to,
               "tasks": selectors.tasks_visible_to}[kind](request.user).values("pk")
    return qs.filter(**{f"{through or 'pk'}__in": visible})


@admin.register(WorkflowDefinition)
class WorkflowDefinitionAdmin(ReadOnlyMixin, SimpleHistoryAdmin):
    list_display = ("name", "code", "organization", "target_type", "is_default", "status", "auto_start_event")
    list_filter = ("status", "target_type", "is_default", "organization")
    search_fields = ("name", "code")

    def get_queryset(self, request):  # noqa: ANN001, ANN201
        return _scope(request, super().get_queryset(request), "definitions")


@admin.register(WorkflowVersion)
class WorkflowVersionAdmin(ReadOnlyMixin, SimpleHistoryAdmin):
    list_display = ("definition", "number", "status", "published_at", "published_by")
    list_filter = ("status",)
    search_fields = ("definition__name", "definition__code", "change_summary")
    list_select_related = ("definition",)

    def get_queryset(self, request):  # noqa: ANN001, ANN201
        return _scope(request, super().get_queryset(request), "versions")


class _ChildAdmin(ReadOnlyMixin, admin.ModelAdmin):
    def get_queryset(self, request):  # noqa: ANN001, ANN201
        return _scope(request, super().get_queryset(request), "versions", through="version")


@admin.register(WorkflowState)
class WorkflowStateAdmin(_ChildAdmin):
    list_display = ("name", "code", "version", "kind", "approval_mode", "due_hours")
    list_filter = ("kind", "approval_mode")
    list_select_related = ("version__definition",)


@admin.register(WorkflowTransition)
class WorkflowTransitionAdmin(_ChildAdmin):
    list_display = ("action", "label", "version", "source", "target", "handler", "actor_rule", "decision", "enabled")
    list_filter = ("actor_rule", "decision", "enabled")
    list_select_related = ("version__definition", "source", "target")


@admin.register(WorkflowInstance)
class WorkflowInstanceAdmin(ReadOnlyMixin, SimpleHistoryAdmin):
    list_display = ("reference", "definition", "version", "target_type", "state", "status", "outcome", "needs_attention", "started_at")
    list_filter = ("status", "outcome", "needs_attention", "target_type")
    list_select_related = ("definition", "version", "state", "organization")
    date_hierarchy = "started_at"

    def get_queryset(self, request):  # noqa: ANN001, ANN201
        return _scope(request, super().get_queryset(request), "instances")


@admin.register(WorkflowTask)
class WorkflowTaskAdmin(ReadOnlyMixin, admin.ModelAdmin):
    list_display = ("instance", "state", "assignee", "status", "due_at", "completed_at")
    list_filter = ("status",)
    list_select_related = ("instance", "state", "assignee")

    def get_queryset(self, request):  # noqa: ANN001, ANN201
        return _scope(request, super().get_queryset(request), "tasks")


@admin.register(WorkflowEvent)
class WorkflowEventAdmin(ReadOnlyMixin, admin.ModelAdmin):
    list_display = ("instance", "seq", "kind", "action", "actor", "created_at")
    list_filter = ("kind",)
    list_select_related = ("instance", "actor")

    def get_queryset(self, request):  # noqa: ANN001, ANN201
        return _scope(request, super().get_queryset(request), "instances", through="instance")
