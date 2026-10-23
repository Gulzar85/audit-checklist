"""Django admin for scoring: a *read-only* inspection tool.

A write here would skip validation, the draft-only rule, the integrity seal and the audit trail (configurations), or rewrite a recorded
result.  Configurations are frozen by database triggers once their version is published and results are append-only, so there is no add /
change / delete and no bulk action.  Non-superusers only see what they may see through the application (the configurations of their
organizations' checklists; the results of the executions in their scope).
"""

from __future__ import annotations

from django.contrib import admin
from simple_history.admin import SimpleHistoryAdmin

from apps.checklists import selectors as checklist_selectors

from . import selectors
from .models import ScoreResult, ScoringConfig


class ReadOnlyMixin:
    actions = None

    def has_add_permission(self, request) -> bool:  # noqa: ANN001
        return False

    def has_change_permission(self, request, obj=None) -> bool:  # noqa: ANN001
        return False

    def has_delete_permission(self, request, obj=None) -> bool:  # noqa: ANN001
        return False


@admin.register(ScoringConfig)
class ScoringConfigAdmin(ReadOnlyMixin, SimpleHistoryAdmin):
    list_display = ("version", "organization", "enabled", "method", "na_policy", "missing_policy", "precision", "updated_at")
    list_filter = ("enabled", "method", "organization")
    search_fields = ("version__template__code", "version__template__name")
    list_select_related = ("version__template", "organization")

    def get_queryset(self, request):  # noqa: ANN001, ANN201
        qs = super().get_queryset(request)
        if request.user.is_superuser:
            return qs
        return qs.filter(version__in=checklist_selectors.versions_visible_to(request.user).values("pk"))


@admin.register(ScoreResult)
class ScoreResultAdmin(ReadOnlyMixin, admin.ModelAdmin):
    list_display = ("execution", "status", "kind", "percentage", "grade", "critical", "is_current", "created_at")
    list_filter = ("status", "kind", "critical", "is_current", "organization")
    search_fields = ("execution__number",)
    list_select_related = ("execution", "organization")
    date_hierarchy = "created_at"

    def get_queryset(self, request):  # noqa: ANN001, ANN201
        qs = super().get_queryset(request)
        return qs if request.user.is_superuser else qs.filter(pk__in=selectors.results_visible_to(request.user).values("pk"))
