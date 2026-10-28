"""The KPI registry (Phase 20).  A KPI is a *pointer into a Phase 19 report* - which report, which grouping, which metric column, which fixed filters - plus its business
definition (numerator, denominator, unit, what an empty scope means).  There is no formula language and no SQL: the number is whatever the authorized report computes."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from django.core.exceptions import ImproperlyConfigured

COUNT, PERCENT, MINUTES = "count", "percent", "minutes"


@dataclass(frozen=True)
class Kpi:
    code: str
    name: str
    description: str
    category: str
    unit: str
    report: str
    metric: str
    numerator: str
    denominator: str = ""
    group_by: str = ""
    fixed_filters: dict[str, Any] = field(default_factory=dict)
    mode: str = "overall"  # "overall" | "score_average" | "top_severity"
    empty: str = "zero"  # "zero": nothing in scope is a real 0 | "none": no data -> blank
    better: str = ""  # "up" | "down" | "" (no judgement)
    version: int = 1
    drill_report: str = ""
    drill_filters: dict[str, Any] = field(default_factory=dict)


REGISTRY: dict[str, Kpi] = {}


def register(kpi: Kpi) -> Kpi:
    if kpi.code in REGISTRY:
        raise ImproperlyConfigured(f"KPI {kpi.code!r} is already registered")
    REGISTRY[kpi.code] = kpi
    return kpi


def get(code: Any) -> Kpi | None:
    return REGISTRY.get(code) if isinstance(code, str) else None


def describe(kpi: Kpi) -> dict[str, Any]:
    return {"code": kpi.code, "name": kpi.name, "description": kpi.description, "category": kpi.category, "unit": kpi.unit, "numerator": kpi.numerator, "denominator": kpi.denominator,
            "empty_scope": "0" if kpi.empty == "zero" else "blank (no data)", "better": kpi.better, "version": kpi.version, "source_report": kpi.report}


for _k in (
    Kpi("assignments_eligible", "Eligible assignments", "Assignments issued in the period and not cancelled.", "Checklists", COUNT, "completion", "eligible", "assignments issued in the period, not cancelled",
        drill_report="assignments"),
    Kpi("assignments_completed", "Completed assignments", "Eligible assignments whose status is completed.", "Checklists", COUNT, "completion", "completed", "eligible assignments with status completed",
        better="up", drill_report="assignments", drill_filters={"status": "completed"}),
    Kpi("completion_rate", "Completion rate", "Completed / eligible assignments issued in the period.", "Checklists", PERCENT, "completion", "completion_pct", "completed assignments",
        "eligible assignments (issued in the period, not cancelled)", empty="none", better="up", drill_report="completion"),
    Kpi("assignments_pending", "Not started", "Eligible assignments still waiting to be started.", "Checklists", COUNT, "completion", "pending", "eligible assignments with status assigned",
        drill_report="assignments", drill_filters={"status": "assigned"}),
    Kpi("assignments_overdue", "Overdue assignments", "Eligible assignments still open (assigned / in progress) whose due date has passed.", "Checklists", COUNT, "completion", "overdue",
        "open eligible assignments past their due date", better="down", drill_report="assignments", drill_filters={"overdue": True}),
    Kpi("executions_submitted", "Executions submitted", "Executions started in the period that have been submitted.", "Checklists", COUNT, "execution_summary", "submitted",
        "executions started in the period with status submitted", better="up", drill_report="executions", drill_filters={"status": "submitted"}),
    Kpi("average_score", "Average score", "Unweighted average of the stored final score percentages - only when the scope holds one checklist (scores of different checklists are not comparable).",
        "Scoring", PERCENT, "score_summary", "average", "sum of the stored final percentages", "number of scored executions", group_by="template", mode="score_average", empty="none",
        better="up", drill_report="scores"),
    Kpi("critical_failures", "Critical failures", "Scored executions flagged with a critical failure by their scoring configuration.", "Scoring", COUNT, "score_summary", "critical",
        "scored executions with a critical failure", better="down", drill_report="scores"),
    Kpi("findings_created", "Findings raised", "Findings created in the period.", "Findings", COUNT, "findings_summary", "created", "findings created in the period", drill_report="findings"),
    Kpi("findings_open", "Open findings", "Findings open now (open, acknowledged, in progress or reopened), whatever their creation date.", "Findings", COUNT, "findings_summary", "open_now",
        "findings in an active status now", better="down", drill_report="findings"),
    Kpi("findings_top_severity", "Open findings at the highest severity", "Open findings at the organization's highest-ranked severity (by rank, not by name).", "Findings", COUNT,
        "findings_summary", "open_now", "open findings at the highest-ranked severity", group_by="severity", mode="top_severity", better="down", drill_report="findings"),
    Kpi("actions_open", "Actions with their owner", "Corrective actions open, in progress, blocked or rejected now.", "Corrective actions", COUNT, "actions_summary", "working_now",
        "actions whose owner still has work to do", drill_report="corrective_actions"),
    Kpi("actions_overdue", "Overdue actions", "Actions whose owner still has work to do and whose due date has passed.", "Corrective actions", COUNT, "actions_summary", "overdue_now",
        "working actions past their due date", better="down", drill_report="corrective_actions", drill_filters={"overdue": True}),
    Kpi("actions_awaiting_verification", "Awaiting verification", "Actions submitted by their owner and not yet verified (submission is not verification).", "Corrective actions", COUNT,
        "actions_summary", "awaiting_verification_now", "actions with status submitted", drill_report="corrective_actions", drill_filters={"status": "submitted"}),
    Kpi("workflow_tasks_open", "Workflow tasks awaiting action", "Pending or claimed tasks of active workflows.", "Workflows", COUNT, "workflow_summary", "open_tasks_now",
        "open tasks of active workflows", better="down", drill_report="workflows", drill_filters={"status": "active"}),
    Kpi("workflow_tasks_overdue", "Overdue workflow tasks", "Open tasks of active workflows whose due date has passed.", "Workflows", COUNT, "workflow_summary", "overdue_tasks_now",
        "open tasks past their due date", better="down", drill_report="workflows", drill_filters={"status": "active"}),
    Kpi("evaluations_attention", "Evaluations needing attention", "Current evaluations with outcome 'fail' or 'review required'.", "Evaluations", COUNT, "evaluation_summary", "runs",
        "current evaluations with outcome fail or review required", group_by="outcome", mode="outcomes:fail,review_required", better="down", drill_report="evaluations",
        drill_filters={"current": True}),
):
    register(_k)
