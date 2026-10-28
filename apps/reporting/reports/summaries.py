"""Summary reports added for Phase 20 (dashboards) - and usable as ordinary reports and exports.

Every summary is a grouped database aggregation over the owning module's *scoped* selector (``apply_common``), so dashboards and reports share one calculation path.
Two kinds of metric, always labelled:

* **period metrics** count records whose date (``date_label``) falls in the selected range;
* **now metrics** (suffix ``_now``) describe the state at generation time for every record in scope, whatever its date.  They are not bucketed by time: when a summary
  is grouped by a period, now-metrics are blank.

Empty time buckets are filled: counts are ``0`` (a real zero), averages and percentages are blank (no data - never zero).
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

from django.db.models import Avg, Count, DurationField, ExpressionWrapper, F, Q, Value
from django.db.models.functions import TruncDay, TruncMonth, TruncQuarter, TruncWeek
from django.utils import timezone

from apps.corrective_actions import selectors as action_selectors
from apps.corrective_actions.constants import ActionStatus, WORKING_STATUSES
from apps.corrective_actions.models import CorrectiveAction
from apps.evaluation import selectors as evaluation_selectors
from apps.evaluation.constants import Outcome
from apps.executions import selectors as execution_selectors
from apps.executions.constants import ExecutionStatus
from apps.findings import selectors as finding_selectors
from apps.findings.constants import ACTIVE_STATUSES as FINDING_ACTIVE
from apps.findings.constants import FindingStatus
from apps.permissions.registry import P
from apps.scoring import selectors as scoring_selectors
from apps.scoring.constants import ResultStatus
from apps.workflows import selectors as workflow_selectors
from apps.workflows.constants import InstanceStatus
from apps.workflows.models import WorkflowTask

from .. import constants as k
from ..registry import DECIMAL, INT, TEXT, Column as C, FilterSpec as F_, Report, register
from ._base import Ctx, apply_common, duration_minutes

TRUNC = {"day": TruncDay, "week": TruncWeek, "month": TruncMonth, "quarter": TruncQuarter}
PERIOD_GROUPS = (("day", "Day"), ("week", "Week"), ("month", "Month"), ("quarter", "Quarter"))
LOCATION = F_("location", "uuid", "Location")
DESCENDANTS = F_("include_descendants", "bool", "Include sub-locations")
OPEN_TASKS = ("pending", "claimed")


def bucket_start(day: date, period: str) -> date:
    if period == "week":
        return day - timedelta(days=day.weekday())  # ISO weeks start on Monday (as Postgres date_trunc('week'))
    if period == "month":
        return day.replace(day=1)
    if period == "quarter":
        return day.replace(month=(day.month - 1) // 3 * 3 + 1, day=1)
    return day


def next_bucket(day: date, period: str) -> date:
    if period == "day":
        return day + timedelta(days=1)
    if period == "week":
        return day + timedelta(days=7)
    months = 1 if period == "month" else 3
    month = day.month - 1 + months
    return date(day.year + month // 12, month % 12 + 1, 1)


def buckets(ctx: Ctx, period: str) -> list[date]:
    """Every bucket start overlapping the selected range ``[start, end)`` in the server time zone."""
    first = timezone.localtime(ctx.params.start).date()
    last = timezone.localtime(ctx.params.end - timedelta(microseconds=1)).date()
    out, current = [], bucket_start(first, period)
    while current <= last and len(out) < k.MAX_SUMMARY_GROUPS:
        out.append(current)
        current = next_bucket(current, period)
    return out


def period_label(day: date, period: str) -> str:
    """Readable, sortable bucket labels: 2026-10-28 (day), 2026-10-26 (week starting Monday), 2026-10 (month), 2026-Q4 (quarter)."""
    if period == "month":
        return day.strftime("%Y-%m")
    if period == "quarter":
        return f"{day.year}-Q{(day.month - 1) // 3 + 1}"
    return day.isoformat()


def _key(value: Any) -> Any:
    if isinstance(value, datetime):
        return timezone.localtime(value).date() if timezone.is_aware(value) else value.date()
    return value


def summarize(ctx: Ctx, *, period_qs, date_path: str, groups: dict[str, tuple[str, str | None, dict | None]], period_metrics: dict[str, Any], now_qs=None,
              now_metrics: dict[str, Any] | None = None, averages: tuple[str, ...] = (), extra=None) -> list[dict[str, Any]]:
    """Group ``period_qs`` (already date-filtered) and ``now_qs`` (not date-filtered) by the requested dimension and merge them into rows.

    ``groups[name] = (key path, label path or None, labels-by-value or None)``.  Each row has ``group`` (label) and ``key`` (the raw value, for drill-down) plus the metrics."""
    group = ctx.params.group_by
    now_metrics = now_metrics or {}
    is_period = group in TRUNC
    tz = timezone.get_default_timezone()

    def grouped(qs, metrics):  # noqa: ANN001, ANN202
        if not metrics:
            return []
        if is_period:
            qs = qs.annotate(gk=TRUNC[group](date_path, tzinfo=tz), gl=Value(""))
        elif group:
            key_path, label_path, _labels = groups[group]
            qs = qs.annotate(gk=F(key_path), gl=F(label_path) if label_path else Value(""))
        else:
            qs = qs.annotate(gk=Value("all"), gl=Value("All"))
        return list(qs.order_by().values("gk", "gl").annotate(**metrics)[: k.MAX_SUMMARY_GROUPS])

    rows: dict[Any, dict[str, Any]] = {}
    for row in grouped(period_qs, period_metrics):
        rows.setdefault(_key(row["gk"]), {"gl": row["gl"]}).update({m: row[m] for m in period_metrics})
    if now_qs is not None and not is_period:
        for row in grouped(now_qs, now_metrics):
            rows.setdefault(_key(row["gk"]), {"gl": row["gl"]}).update({m: row[m] for m in now_metrics})
    if is_period:
        for start in buckets(ctx, group):
            rows.setdefault(start, {"gl": ""})
    labels = groups.get(group, (None, None, None))[2] if group and not is_period else None
    out = []
    for key, values in rows.items():
        label = period_label(key, group) if isinstance(key, date) else (labels.get(key, key) if labels else (values.get("gl") or (str(key) if key is not None else "(none)")))
        item = {"group": label, "key": key.isoformat() if isinstance(key, date) else (str(key) if key is not None else "")}
        for metric in period_metrics:
            item[metric] = values.get(metric, None if metric in averages else 0)
        for metric in now_metrics:
            item[metric] = None if is_period else values.get(metric, None if metric in averages else 0)
        for metric in averages:
            if isinstance(item.get(metric), timedelta):
                item[metric] = duration_minutes(item[metric])
            elif item.get(metric) is not None:
                item[metric] = round(float(item[metric]), 2)
        if extra:
            extra(item)
        out.append(item)
    if is_period:
        out.sort(key=lambda r: r["key"])
    return out


def _choices(model_choices: Any) -> dict[str, str]:
    return {str(v): str(l) for v, l in model_choices}


# ------------------------------------------------------------------------------------------------------------------------------ executions
def build_execution_summary(ctx: Ctx) -> list[dict[str, Any]]:
    base = execution_selectors.executions_visible_to(ctx.user).filter(organization=ctx.organization)
    period = apply_common(ctx, base, date_field="started_at", location_path="assignment__location_id")
    duration = ExpressionWrapper(F("submitted_at") - F("started_at"), output_field=DurationField())
    groups = {"status": ("status", None, _choices(ExecutionStatus.choices)), "template": ("version__template_id", "version__template__name", None),
              "version": ("version_id", "version__version_number", None), "location": ("assignment__location_id", "assignment__location__name", None)}
    return summarize(ctx, period_qs=period, date_path="started_at", groups=groups, averages=("avg_minutes",),
                     period_metrics={"started": Count("pk"), "submitted": Count("pk", filter=Q(status=ExecutionStatus.SUBMITTED)),
                                     "in_progress": Count("pk", filter=Q(status=ExecutionStatus.IN_PROGRESS)), "cancelled": Count("pk", filter=Q(status=ExecutionStatus.CANCELLED)),
                                     "avg_minutes": Avg(duration, filter=Q(status=ExecutionStatus.SUBMITTED))})


register(Report(
    code="execution_summary", version=1, title="Execution summary", category="Checklists", kind="summary", build=build_execution_summary, date_field="started_at", date_label="the execution start time",
    description="Executions started in the period by status, checklist, version, location or time: started, submitted, in progress, cancelled and the average duration of submitted executions.",
    columns=(C("group", "Group", TEXT), C("started", "Started", INT), C("submitted", "Submitted", INT), C("in_progress", "In progress", INT), C("cancelled", "Cancelled", INT),
             C("avg_minutes", "Avg duration (min)", DECIMAL)),
    filters=(LOCATION, DESCENDANTS, F_("template", "uuid", "Checklist template", "version__template_id"), F_("version", "uuid", "Published version", "version_id")),
    group_by=(("status", "Status"), ("template", "Checklist"), ("version", "Version"), ("location", "Location"), *PERIOD_GROUPS), default_sort="group",
    source_permission=P.EXECUTION_VIEW))


# ------------------------------------------------------------------------------------------------------------------------------ scores
BANDS = [(0, 50), (50, 60), (60, 70), (70, 80), (80, 90), (90, 101)]


def build_score_distribution(ctx: Ctx) -> list[dict[str, Any]]:
    """How many scored executions fall in each grade, or in each 10-point percentage band (the stored percentages; never recalculated)."""
    qs = scoring_selectors.results_visible_to(ctx.user).filter(organization=ctx.organization, is_current=True, status=ResultStatus.FINAL)
    qs = apply_common(ctx, qs, date_field="execution__submitted_at", location_path="execution__assignment__location_id", permission=P.SCORING_SCORE_VIEW)
    if ctx.params.group_by == "grade":
        rows = qs.order_by().values("grade").annotate(n=Count("pk"))
        return sorted(({"group": r["grade"] or "(no grade)", "key": r["grade"] or "", "scored": r["n"]} for r in rows), key=lambda r: r["group"])
    metrics = {f"b{lo}": Count("pk", filter=Q(percentage__gte=lo, percentage__lt=hi)) for lo, hi in BANDS}
    totals = qs.aggregate(**metrics)
    return [{"group": f"{lo}-{min(hi, 100)}%", "key": str(lo), "scored": totals[f"b{lo}"]} for lo, hi in BANDS]


register(Report(
    code="score_distribution", version=1, title="Score distribution", category="Scoring", kind="summary", build=build_score_distribution, date_field="execution__submitted_at",
    date_label="the execution submission time",
    description="Stored final score snapshots counted by percentage band or by grade. Unscored executions are not counted. Mixing checklists mixes scales: filter by checklist to compare like with like.",
    columns=(C("group", "Band / grade", TEXT), C("scored", "Scored executions", INT)),
    filters=(LOCATION, DESCENDANTS, F_("template", "uuid", "Checklist template", "version__template_id"), F_("version", "uuid", "Published version", "version_id")),
    group_by=(("band", "10-point band"), ("grade", "Grade")), default_sort="group", source_permission=P.SCORING_SCORE_VIEW))


def build_score_trend(ctx: Ctx) -> list[dict[str, Any]]:
    qs = scoring_selectors.results_visible_to(ctx.user).filter(organization=ctx.organization, is_current=True, status=ResultStatus.FINAL)
    period = apply_common(ctx, qs, date_field="execution__submitted_at", location_path="execution__assignment__location_id", permission=P.SCORING_SCORE_VIEW)
    groups = {"template": ("version__template_id", "version__template__name", None), "location": ("execution__assignment__location_id", "execution__assignment__location__name", None)}
    return summarize(ctx, period_qs=period, date_path="execution__submitted_at", groups=groups, averages=("average",),
                     period_metrics={"scored": Count("pk"), "average": Avg("percentage"), "critical": Count("pk", filter=Q(critical=True))})


register(Report(
    code="score_trend", version=1, title="Score trend", category="Scoring", kind="summary", build=build_score_trend, date_field="execution__submitted_at", date_label="the execution submission time",
    description="Scored executions, unweighted average of the stored score percentages and critical failures per day, week, month or quarter (or per checklist / location). Empty periods have no average, not zero.",
    columns=(C("group", "Group", TEXT), C("scored", "Scored executions", INT), C("average", "Average %", DECIMAL), C("critical", "Critical failures", INT)),
    filters=(LOCATION, DESCENDANTS, F_("template", "uuid", "Checklist template", "version__template_id"), F_("version", "uuid", "Published version", "version_id")),
    group_by=(*PERIOD_GROUPS, ("template", "Checklist"), ("location", "Location")), default_sort="group", source_permission=P.SCORING_SCORE_VIEW))


# ------------------------------------------------------------------------------------------------------------------------------ findings
def build_findings_summary(ctx: Ctx) -> list[dict[str, Any]]:
    base = finding_selectors.findings_visible_to(ctx.user, ctx.organization)
    period = apply_common(ctx, base, date_field="created_at")
    now = apply_common(ctx, base)
    groups = {"status": ("status", None, _choices(FindingStatus.choices)), "severity": ("severity_id", "severity__name", None), "category": ("category_id", "category__name", None),
              "location": ("location_id", "location__name", None)}
    return summarize(ctx, period_qs=period, date_path="created_at", groups=groups, period_metrics={"created": Count("pk")}, now_qs=now,
                     now_metrics={"open_now": Count("pk", filter=Q(status__in=[s.value for s in FINDING_ACTIVE]))})


register(Report(
    code="findings_summary", version=1, title="Findings summary", category="Quality", kind="summary", build=build_findings_summary, date_field="created_at", date_label="the finding creation time",
    description="Findings created in the period, and findings open now (open, acknowledged, in progress or reopened), by status, severity, category, location or time.",
    columns=(C("group", "Group", TEXT), C("created", "Created in period", INT), C("open_now", "Open now", INT)),
    filters=(LOCATION, DESCENDANTS, F_("severity", "uuid", "Severity", "severity_id"), F_("category", "uuid", "Category", "category_id"),
             F_("status", "choice", "Status", "status", tuple(_choices(FindingStatus.choices).items()))),
    group_by=(("status", "Status"), ("severity", "Severity"), ("category", "Category"), ("location", "Location"), *PERIOD_GROUPS), default_sort="group", source_permission=P.FINDING_VIEW))


# ------------------------------------------------------------------------------------------------------------------------------ corrective actions
def build_actions_summary(ctx: Ctx) -> list[dict[str, Any]]:
    base = action_selectors.actions_visible_to(ctx.user, ctx.organization)
    period = apply_common(ctx, base, date_field="created_at")
    now = apply_common(ctx, base)
    working = [s.value for s in WORKING_STATUSES]
    groups = {"status": ("status", None, _choices(ActionStatus.choices)), "priority": ("priority", None, _choices(CorrectiveAction._meta.get_field("priority").choices)),
              "location": ("location_id", "location__name", None)}
    return summarize(ctx, period_qs=period, date_path="created_at", groups=groups,
                     period_metrics={"created": Count("pk"), "verified": Count("pk", filter=Q(verified_at__isnull=False)), "closed": Count("pk", filter=Q(status=ActionStatus.CLOSED))},
                     now_qs=now, now_metrics={"working_now": Count("pk", filter=Q(status__in=working)), "awaiting_verification_now": Count("pk", filter=Q(status=ActionStatus.SUBMITTED)),
                                              "awaiting_closure_now": Count("pk", filter=Q(status=ActionStatus.VERIFIED)),
                                              "overdue_now": Count("pk", filter=Q(status__in=working, due_at__lt=ctx.now))})


register(Report(
    code="actions_summary", version=1, title="Corrective action summary", category="Quality", kind="summary", build=build_actions_summary, date_field="created_at", date_label="the action creation time",
    description="Actions created, verified and closed (period) and, now: with the owner, awaiting verification (submitted), verified awaiting closure, overdue. Submission is not verification; verification is not closure.",
    columns=(C("group", "Group", TEXT), C("created", "Created", INT), C("verified", "Verified", INT), C("closed", "Closed", INT), C("working_now", "With the owner (now)", INT),
             C("awaiting_verification_now", "Awaiting verification (now)", INT), C("awaiting_closure_now", "Verified, awaiting closure (now)", INT), C("overdue_now", "Overdue (now)", INT)),
    filters=(LOCATION, DESCENDANTS, F_("priority", "choice", "Priority", "priority", tuple(_choices(CorrectiveAction._meta.get_field("priority").choices).items()))),
    group_by=(("status", "Status"), ("priority", "Priority"), ("location", "Location"), *PERIOD_GROUPS), default_sort="group", source_permission=P.CORRECTIVE_ACTION_VIEW))


# ------------------------------------------------------------------------------------------------------------------------------ workflows
def build_workflow_summary(ctx: Ctx) -> list[dict[str, Any]]:
    base = workflow_selectors.instances_visible_to(ctx.user).filter(organization=ctx.organization)
    period = apply_common(ctx, base, date_field="started_at")
    now = apply_common(ctx, base)
    groups = {"definition": ("definition_id", "definition__name", None), "state": ("state_id", "state__name", None), "location": ("location_id", "location__name", None),
              "status": ("status", None, _choices(InstanceStatus.choices))}
    rows = summarize(ctx, period_qs=period, date_path="started_at", groups=groups,
                     period_metrics={"started": Count("pk"), "completed": Count("pk", filter=Q(status=InstanceStatus.COMPLETED))},
                     now_qs=now, now_metrics={"active_now": Count("pk", filter=Q(status=InstanceStatus.ACTIVE))})
    if ctx.params.group_by in TRUNC:
        return rows
    tasks = WorkflowTask.objects.filter(instance__in=now.filter(status=InstanceStatus.ACTIVE).values("pk"), status__in=OPEN_TASKS)
    if ctx.params.group_by:
        tasks = tasks.annotate(gk=F(f"instance__{groups[ctx.params.group_by][0]}"))
    else:
        tasks = tasks.annotate(gk=Value("all"))
    counts = {str(r["gk"]): r for r in tasks.order_by().values("gk").annotate(open=Count("pk"), overdue=Count("pk", filter=Q(due_at__lt=ctx.now)))}
    for row in rows:
        hit = counts.get(row["key"] or "None", {})
        row["open_tasks_now"], row["overdue_tasks_now"] = hit.get("open", 0), hit.get("overdue", 0)
    return rows


register(Report(
    code="workflow_summary", version=1, title="Workflow summary", category="Quality", kind="summary", build=build_workflow_summary, date_field="started_at", date_label="the workflow start time",
    description="Workflows started and completed (period); active workflows, open tasks (pending or claimed) and overdue open tasks now, by definition, current state, location, status or time.",
    columns=(C("group", "Group", TEXT), C("started", "Started", INT), C("completed", "Completed", INT), C("active_now", "Active (now)", INT), C("open_tasks_now", "Open tasks (now)", INT),
             C("overdue_tasks_now", "Overdue tasks (now)", INT)),
    filters=(LOCATION, DESCENDANTS, F_("definition", "uuid", "Workflow definition", "definition_id")),
    group_by=(("definition", "Definition"), ("state", "Current state"), ("location", "Location"), ("status", "Status"), *PERIOD_GROUPS), default_sort="group",
    source_permission=P.WORKFLOW_INSTANCE_VIEW))


# ------------------------------------------------------------------------------------------------------------------------------ evaluations
def build_evaluation_summary(ctx: Ctx) -> list[dict[str, Any]]:
    base = evaluation_selectors.runs_visible_to(ctx.user).filter(organization=ctx.organization)
    if not ctx.params.filters.get("include_history"):
        base = base.filter(is_current=True)
    period = apply_common(ctx, base, date_field="created_at", location_path="execution__assignment__location_id")
    groups = {"outcome": ("outcome", None, _choices(Outcome.choices)), "template": ("version__template_id", "version__template__name", None),
              "location": ("execution__assignment__location_id", "execution__assignment__location__name", None)}
    return summarize(ctx, period_qs=period, date_path="created_at", groups=groups, period_metrics={"runs": Count("pk")})


register(Report(
    code="evaluation_summary", version=1, title="Evaluation summary", category="Quality", kind="summary", build=build_evaluation_summary, date_field="created_at", date_label="the evaluation time",
    description="Stored evaluation runs by outcome (the engine's own outcome list), checklist, location or time. Current evaluations only unless history is included; nothing is re-run.",
    columns=(C("group", "Group", TEXT), C("runs", "Evaluations", INT)),
    filters=(LOCATION, DESCENDANTS, F_("template", "uuid", "Checklist template", "version__template_id"), F_("outcome", "choice", "Outcome", "outcome", tuple(_choices(Outcome.choices).items())),
             F_("include_history", "bool", "Include superseded evaluations")),
    group_by=(("outcome", "Outcome"), ("template", "Checklist"), ("location", "Location"), *PERIOD_GROUPS), default_sort="group", source_permission=P.EVALUATION_RESULT_VIEW))
