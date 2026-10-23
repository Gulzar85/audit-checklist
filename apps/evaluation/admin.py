"""Django admin for evaluation: a *read-only* inspection tool.

A write here would skip validation, the draft-only rule, the integrity seal and the audit trail (rule sets), or rewrite a recorded run / action outcome.  Rule sets are
frozen by database triggers once their version is published, runs are append-only and action requests are write-once, so there is no add / change / delete and no bulk
action.  Non-superusers only see what they may see through the application (the rule sets of their organizations' checklists; the runs and action requests of the
executions in their scope).
"""

from __future__ import annotations

from django.contrib import admin
from simple_history.admin import SimpleHistoryAdmin

from apps.checklists import selectors as checklist_selectors

from . import selectors
from .models import EvaluationActionRequest, EvaluationRuleSet, EvaluationRun


class ReadOnlyMixin:
    actions = None

    def has_add_permission(self, request) -> bool:  # noqa: ANN001
        return False

    def has_change_permission(self, request, obj=None) -> bool:  # noqa: ANN001
        return False

    def has_delete_permission(self, request, obj=None) -> bool:  # noqa: ANN001
        return False


@admin.register(EvaluationRuleSet)
class EvaluationRuleSetAdmin(ReadOnlyMixin, SimpleHistoryAdmin):
    list_display = ("version", "organization", "enabled", "updated_at")
    list_filter = ("enabled", "organization")
    search_fields = ("version__template__code", "version__template__name")
    list_select_related = ("version__template", "organization")

    def get_queryset(self, request):  # noqa: ANN001, ANN201
        qs = super().get_queryset(request)
        return qs if request.user.is_superuser else qs.filter(version__in=checklist_selectors.versions_visible_to(request.user).values("pk"))


@admin.register(EvaluationRun)
class EvaluationRunAdmin(ReadOnlyMixin, admin.ModelAdmin):
    list_display = ("execution", "outcome", "trigger", "is_current", "created_by", "created_at")
    list_filter = ("outcome", "trigger", "is_current", "organization")
    search_fields = ("execution__number",)
    list_select_related = ("execution", "organization", "created_by")
    date_hierarchy = "created_at"

    def get_queryset(self, request):  # noqa: ANN001, ANN201
        qs = super().get_queryset(request)
        return qs if request.user.is_superuser else qs.filter(pk__in=selectors.runs_visible_to(request.user).values("pk"))


@admin.register(EvaluationActionRequest)
class EvaluationActionRequestAdmin(ReadOnlyMixin, admin.ModelAdmin):
    list_display = ("rule_code", "action_type", "status", "execution", "result_type", "finished_at")
    list_filter = ("status", "action_type", "organization")
    search_fields = ("rule_code", "action_key", "execution__number")
    list_select_related = ("execution", "organization")

    def get_queryset(self, request):  # noqa: ANN001, ANN201
        qs = super().get_queryset(request)
        return qs if request.user.is_superuser else qs.filter(run__in=selectors.runs_visible_to(request.user).values("pk"))
