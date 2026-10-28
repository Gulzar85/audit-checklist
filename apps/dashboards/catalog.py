"""The widget registry and the dashboard catalogue (Phase 20).  Users can only choose among these codes; layouts store codes, never markup or configuration code."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from django.core.exceptions import ImproperlyConfigured

KPI, TREND, BARS, DONUT, TABLE = "kpi", "trend", "bars", "donut", "table"


@dataclass(frozen=True)
class Metric:
    key: str
    label: str
    unit: str = "count"


@dataclass(frozen=True)
class Widget:
    code: str
    title: str
    kind: str
    description: str = ""
    kpi: str = ""  # KPI widgets
    report: str = ""  # chart / table widgets: the Phase 19 summary report
    group_by: str = ""  # "period" = the dashboard's time bucket
    metrics: tuple[Metric, ...] = ()
    fixed_filters: dict[str, Any] = field(default_factory=dict)
    max_items: int = 12
    drill_report: str = ""
    drill_key: str = ""  # the target report filter that receives a row's key (a category drill-down)
    size: int = 1  # grid columns (1-4)


@dataclass(frozen=True)
class Dashboard:
    code: str
    title: str
    description: str
    icon: str
    widgets: tuple[str, ...]


WIDGETS: dict[str, Widget] = {}
DASHBOARDS: dict[str, Dashboard] = {}

M = Metric


def _w(*widgets: Widget) -> None:
    for widget in widgets:
        if widget.code in WIDGETS:
            raise ImproperlyConfigured(f"widget {widget.code!r} is already registered")
        WIDGETS[widget.code] = widget


def kpi_widget(code: str, size: int = 1) -> Widget:
    return Widget(f"kpi_{code}", "", KPI, kpi=code, size=size)


_w(*[kpi_widget(c) for c in ("assignments_eligible", "assignments_completed", "completion_rate", "assignments_pending", "assignments_overdue", "executions_submitted", "average_score",
                             "critical_failures", "findings_created", "findings_open", "findings_top_severity", "actions_open", "actions_overdue", "actions_awaiting_verification",
                             "workflow_tasks_open", "workflow_tasks_overdue", "evaluations_attention")])
_w(
    Widget("completion_trend", "Completion over time", TREND, "Assignments issued per period and how many are completed.", report="completion", group_by="period",
           metrics=(M("eligible", "Eligible"), M("completed", "Completed")), drill_report="completion", size=2),
    Widget("completion_by_location", "Completion by location", BARS, "Completion rate of assignments issued in the period, per location.", report="completion", group_by="location",
           metrics=(M("completion_pct", "Completion %", "percent"),), drill_report="assignments", size=2),
    Widget("completion_by_template", "Completion by checklist", BARS, "Completion rate per checklist.", report="completion", group_by="template",
           metrics=(M("completion_pct", "Completion %", "percent"),), drill_report="assignments", drill_key="template", size=2),
    Widget("execution_status", "Execution status", DONUT, "Executions started in the period by status.", report="execution_summary", group_by="status", metrics=(M("started", "Executions"),),
           drill_report="executions", drill_key="status"),
    Widget("execution_trend", "Executions over time", TREND, "Executions started and submitted per period.", report="execution_summary", group_by="period",
           metrics=(M("started", "Started"), M("submitted", "Submitted")), drill_report="executions", size=2),
    Widget("execution_duration_by_template", "Average duration by checklist", BARS, "Average minutes from start to submission, per checklist.", report="execution_summary", group_by="template",
           metrics=(M("avg_minutes", "Avg minutes", "minutes"),), drill_report="executions", drill_key="template", size=2),
    Widget("score_trend", "Score trend", TREND, "Unweighted average of stored score percentages per period. Select a checklist to compare like with like.", report="score_trend", group_by="period",
           metrics=(M("average", "Average %", "percent"),), drill_report="scores", size=2),
    Widget("score_by_template", "Average score by checklist", BARS, "Each checklist on its own scale.", report="score_summary", group_by="template", metrics=(M("average", "Average %", "percent"),),
           drill_report="scores", drill_key="template", size=2),
    Widget("score_by_location", "Average score by location", BARS, "Mixes checklists unless a checklist is selected.", report="score_summary", group_by="location",
           metrics=(M("average", "Average %", "percent"),), drill_report="scores", size=2),
    Widget("score_distribution", "Score distribution", BARS, "Scored executions per 10-point band.", report="score_distribution", group_by="band", metrics=(M("scored", "Executions"),),
           drill_report="scores", size=2),
    Widget("grade_distribution", "Grades", DONUT, "Scored executions per grade (as configured by each checklist).", report="score_distribution", group_by="grade", metrics=(M("scored", "Executions"),),
           drill_report="scores"),
    Widget("findings_by_severity", "Open findings by severity", BARS, "Findings open now, per severity.", report="findings_summary", group_by="severity", metrics=(M("open_now", "Open"),),
           drill_report="findings", drill_key="severity", size=2),
    Widget("findings_by_status", "Findings by status", DONUT, "Findings created in the period, by current status.", report="findings_summary", group_by="status", metrics=(M("created", "Findings"),),
           drill_report="findings", drill_key="status"),
    Widget("findings_by_category", "Findings by category", BARS, "Findings created in the period, per category.", report="findings_summary", group_by="category", metrics=(M("created", "Findings"),),
           drill_report="findings", drill_key="category", size=2),
    Widget("findings_trend", "Findings over time", TREND, "Findings created per period.", report="findings_summary", group_by="period", metrics=(M("created", "Created"),), drill_report="findings", size=2),
    Widget("findings_by_location", "Open findings by location", BARS, "Findings open now, per location.", report="findings_summary", group_by="location", metrics=(M("open_now", "Open"),),
           drill_report="findings", size=2),
    Widget("actions_by_status", "Actions by status", DONUT, "Corrective actions created in the period, by status.", report="actions_summary", group_by="status", metrics=(M("created", "Actions"),),
           drill_report="corrective_actions", drill_key="status"),
    Widget("actions_by_priority", "Open actions by priority", BARS, "Actions with their owner now, per priority, and how many are overdue.", report="actions_summary", group_by="priority",
           metrics=(M("working_now", "With owner"), M("overdue_now", "Overdue")), drill_report="corrective_actions", drill_key="priority", size=2),
    Widget("actions_trend", "Actions over time", TREND, "Actions created and verified per period.", report="actions_summary", group_by="period", metrics=(M("created", "Created"), M("verified", "Verified")),
           drill_report="corrective_actions", size=2),
    Widget("workflows_by_state", "Active workflows by state", BARS, "Where active workflows are waiting now.", report="workflow_summary", group_by="state",
           metrics=(M("active_now", "Active"), M("open_tasks_now", "Open tasks")), drill_report="workflows", size=2),
    Widget("workflows_by_definition", "Workflows by definition", BARS, "Started and completed in the period, per definition.", report="workflow_summary", group_by="definition",
           metrics=(M("started", "Started"), M("completed", "Completed")), drill_report="workflows", drill_key="definition", size=2),
    Widget("workflows_trend", "Workflows over time", TREND, "Workflows started and completed per period.", report="workflow_summary", group_by="period",
           metrics=(M("started", "Started"), M("completed", "Completed")), drill_report="workflows", size=2),
    Widget("evaluation_outcomes", "Evaluation outcomes", DONUT, "Current evaluations in the period by outcome.", report="evaluation_summary", group_by="outcome", metrics=(M("runs", "Evaluations"),),
           drill_report="evaluations", drill_key="outcome"),
    Widget("evaluation_trend", "Evaluations over time", TREND, "Current evaluations per period.", report="evaluation_summary", group_by="period", metrics=(M("runs", "Evaluations"),),
           drill_report="evaluations", size=2),
    Widget("evaluation_by_template", "Evaluations by checklist", BARS, "Current evaluations per checklist.", report="evaluation_summary", group_by="template", metrics=(M("runs", "Evaluations"),),
           drill_report="evaluations", drill_key="template", size=2),
    Widget("location_comparison", "Location comparison", TABLE, "Each location on its own (no roll-up). Period and 'now' metrics are labelled.", report="location_comparison",
           metrics=(M("eligible", "Assignments"), M("completion_pct", "Completion %", "percent"), M("overdue", "Overdue"), M("submitted", "Submitted"), M("avg_score", "Avg score %", "percent"),
                    M("open_findings", "Open findings (now)"), M("open_actions", "Open actions (now)"), M("overdue_actions", "Overdue actions (now)"), M("workflow_backlog", "Active workflows (now)")),
           max_items=50, drill_report="location_comparison", size=4),
)


def _d(*dashboards: Dashboard) -> None:
    for dashboard in dashboards:
        missing = [w for w in dashboard.widgets if w not in WIDGETS]
        if missing or len(set(dashboard.widgets)) != len(dashboard.widgets):
            raise ImproperlyConfigured(f"dashboard {dashboard.code!r}: unknown or duplicate widgets {missing}")
        DASHBOARDS[dashboard.code] = dashboard


_d(
    Dashboard("overview", "Executive overview", "Completion, scores, findings, actions and workflow backlog at a glance.", "layout-dashboard",
              ("kpi_completion_rate", "kpi_assignments_overdue", "kpi_average_score", "kpi_findings_open", "kpi_findings_top_severity", "kpi_actions_open", "kpi_actions_overdue",
               "kpi_workflow_tasks_open", "completion_trend", "findings_by_severity", "location_comparison")),
    Dashboard("operations", "Checklist operations", "Assignments and executions: volume, completion, overdue work and duration.", "list-checks",
              ("kpi_assignments_eligible", "kpi_assignments_completed", "kpi_completion_rate", "kpi_assignments_overdue", "kpi_assignments_pending", "kpi_executions_submitted",
               "completion_trend", "execution_status", "completion_by_template", "completion_by_location", "execution_trend", "execution_duration_by_template")),
    Dashboard("scores", "Scores and performance", "Stored score snapshots: averages per checklist, trend, distribution, grades and critical failures.", "gauge",
              ("kpi_average_score", "kpi_critical_failures", "kpi_executions_submitted", "score_trend", "score_by_template", "score_distribution", "grade_distribution", "score_by_location")),
    Dashboard("findings", "Findings", "Raised and open findings by severity, status, category, location and time.", "triangle-alert",
              ("kpi_findings_created", "kpi_findings_open", "kpi_findings_top_severity", "findings_by_severity", "findings_by_status", "findings_trend", "findings_by_category", "findings_by_location")),
    Dashboard("actions", "Corrective actions", "Open, overdue and awaiting-verification actions; status, priority and trend.", "wrench",
              ("kpi_actions_open", "kpi_actions_overdue", "kpi_actions_awaiting_verification", "actions_by_status", "actions_by_priority", "actions_trend")),
    Dashboard("workflows", "Workflows and approvals", "Active workflows, open and overdue tasks, and where work is waiting.", "git-branch",
              ("kpi_workflow_tasks_open", "kpi_workflow_tasks_overdue", "workflows_by_state", "workflows_by_definition", "workflows_trend")),
    Dashboard("evaluations", "Automated evaluations", "Stored evaluation outcomes by outcome, checklist and time.", "gavel",
              ("kpi_evaluations_attention", "evaluation_outcomes", "evaluation_trend", "evaluation_by_template")),
    Dashboard("locations", "Locations", "Side-by-side metrics for each location you may see.", "map-pin",
              ("kpi_completion_rate", "kpi_findings_open", "kpi_actions_overdue", "location_comparison", "completion_by_location", "findings_by_location", "score_by_location")),
)
