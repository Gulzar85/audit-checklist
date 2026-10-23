"""Scoped reads of evaluation data (the choke point shared by the UI, the API and the admin)."""

from __future__ import annotations

from typing import Any

from django.db.models import QuerySet

from apps.core.policies import is_active_user
from apps.core.utils import parse_uuid
from apps.executions import selectors as execution_selectors
from apps.executions.models import ChecklistExecution

from .models import EvaluationActionRequest, EvaluationRuleSet, EvaluationRun


def get_ruleset(version: Any) -> EvaluationRuleSet | None:
    return EvaluationRuleSet.objects.filter(version=version).select_related("version__template").first()


def current_run(execution: ChecklistExecution) -> EvaluationRun | None:
    return EvaluationRun.objects.filter(execution=execution, is_current=True).select_related("created_by", "score_result").first()


def runs_for(execution: ChecklistExecution) -> QuerySet[EvaluationRun]:
    """Every run of the execution, newest first (the original and any re-evaluations)."""
    return EvaluationRun.objects.filter(execution=execution).select_related("created_by").order_by("-created_at")


def actions_for(run: EvaluationRun) -> QuerySet[EvaluationActionRequest]:
    return EvaluationActionRequest.objects.filter(run=run).order_by("rule_code", "action_index")


def runs_visible_to(user: Any) -> QuerySet[EvaluationRun]:
    if not is_active_user(user):
        return EvaluationRun.objects.none()
    return EvaluationRun.objects.filter(execution__in=execution_selectors.executions_visible_to(user).values("pk"))


def get_run(user: Any, pk: Any) -> EvaluationRun | None:
    parsed = parse_uuid(pk)
    return runs_visible_to(user).filter(pk=parsed).select_related("execution", "created_by", "ruleset").first() if parsed else None
