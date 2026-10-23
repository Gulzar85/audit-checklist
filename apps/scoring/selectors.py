"""Scoped reads of scoring data (the choke point shared by the UI, the API and the admin)."""

from __future__ import annotations

from typing import Any

from django.db.models import QuerySet

from apps.core.policies import is_active_user
from apps.executions import selectors as execution_selectors
from apps.executions.models import ChecklistExecution

from .models import ScoreResult, ScoringConfig


def get_config(version: Any) -> ScoringConfig | None:
    return ScoringConfig.objects.filter(version=version).select_related("version__template").first()


def current_result(execution: ChecklistExecution) -> ScoreResult | None:
    return ScoreResult.objects.filter(execution=execution, is_current=True).first()


def results_for(execution: ChecklistExecution) -> QuerySet[ScoreResult]:
    """Every result of the execution, newest first (the history of authorized recalculations)."""
    return ScoreResult.objects.filter(execution=execution).select_related("created_by").order_by("-created_at")


def results_visible_to(user: Any) -> QuerySet[ScoreResult]:
    """Results of the executions the user may see (the scoring permission is checked per object by the policies)."""
    if not is_active_user(user):
        return ScoreResult.objects.none()
    return ScoreResult.objects.filter(execution__in=execution_selectors.executions_visible_to(user).values("pk"))


def configs_in(organization_ids: list[Any]) -> QuerySet[ScoringConfig]:
    return ScoringConfig.objects.filter(organization_id__in=organization_ids).select_related("version__template")
