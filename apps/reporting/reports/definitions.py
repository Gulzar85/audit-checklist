"""The registered reports (Phase 19).  Each builder reads through the owning module's *scoped* selector and the stored business facts - it never recomputes scores,
statuses or evaluations, and never writes.  Column expressions are the only place ORM paths appear; user input only selects keys.

Report-to-data mapping (see docs/reporting-architecture.md): executions, scores, score_summary, evaluations <- ChecklistExecution / ScoreResult (current final snapshot) /
EvaluationRun; completion, assignments <- ChecklistAssignment (+ executions); findings <- Finding; corrective_actions <- CorrectiveAction; workflows <- WorkflowInstance /
WorkflowTask; location_comparison <- the above per location.  *Evidence* is not registered (see the docs: blocked by the absence of a bulk, scoped evidence selector).
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from django.db.models import Avg, Case, CharField, Count, DurationField, ExpressionWrapper, F, IntegerField, Max, Min, OuterRef, Q, Subquery, Value, When
from django.db.models.functions import Coalesce, Concat, Now, TruncMonth
from django.utils import timezone

from apps.assignments import selectors as assignment_selectors
from apps.assignments.constants import AssignmentStatus, OPEN_STATUSES as ASSIGNMENT_OPEN
from apps.corrective_actions import selectors as action_selectors
from apps.corrective_actions.constants import ActionStatus, WORKING_STATUSES
from apps.corrective_actions.models import CorrectiveAction
from apps.evaluation import selectors as evaluation_selectors
from apps.evaluation.constants import Outcome
from apps.evaluation.models import EvaluationRun
from apps.executions import selectors as execution_selectors
from apps.executions.constants import ExecutionStatus
from apps.findings import selectors as finding_selectors
from apps.findings.constants import ACTIVE_STATUSES as FINDING_ACTIVE
from apps.locations.models import Location
from apps.permissions.registry import P
from apps.scoring import selectors as scoring_selectors
from apps.scoring.constants import ResultStatus
from apps.scoring.models import ScoreResult
from apps.workflows import selectors as workflow_selectors
from apps.workflows.constants import InstanceStatus
from apps.workflows.models import WorkflowTask

from .. import constants as k
from ..registry import BOOL, DATE, DATETIME, DECIMAL, INT, TEXT, Column as C, FilterSpec as F_, Report, register
from ._base import Ctx, apply_common, build_list, columns_for, duration_minutes, project

DURATION = "duration"


def _choices(model_choices: Any) -> tuple[tuple[str, str], ...]:
    return tuple((str(v), str(l)) for v, l in model_choices)


STATUS_ASSIGNMENT = _choices(AssignmentStatus.choices)
PRIORITY_ASSIGNMENT = _choices(__import__("apps.assignments.constants", fromlist=["AssignmentPriority"]).AssignmentPriority.choices)
STATUS_EXEC = _choices(ExecutionStatus.choices)
STATUS_ACTION = _choices(ActionStatus.choices)
PRIORITY_ACTION = _choices(CorrectiveAction._meta.get_field("priority").choices)
STATUS_FINDING = _choices(__import__("apps.findings.constants", fromlist=["FindingStatus"]).FindingStatus.choices)
STATUS_INSTANCE = _choices(InstanceStatus.choices)
OUTCOMES = _choices(Outcome.choices)

LOCATION = F_("location", "uuid", "Location")
DESCENDANTS = F_("include_descendants", "bool", "Include sub-locations")
TEMPLATE = lambda path: F_("template", "uuid", "Checklist template", path)  # noqa: E731
ASSIGNEE = lambda path: F_("assigned_to", "uuid", "Assigned to", path, people=True)  # noqa: E731


def _score_sub(field: str, loc_path: str = "execution__assignment__location_id"):
    """The current **final** score snapshot's ``field`` (never recomputed), only where the user may view scores."""
    def build(ctx: Ctx):  # noqa: ANN202
        return Subquery(ScoreResult.objects.filter(execution=OuterRef("pk"), is_current=True, status=ResultStatus.FINAL, **{f"{loc_path}__in": ctx.locations_for(P.SCORING_SCORE_VIEW).values("pk")})
                        .order_by().values(field)[:1])
    return build


def _eval_sub(ctx: Ctx):  # noqa: ANN202
    return Subquery(EvaluationRun.objects.filter(execution=OuterRef("pk"), is_current=True, execution__assignment__location__in=ctx.locations_for(P.EVALUATION_RESULT_VIEW).values("pk"))
                    .order_by().values("outcome")[:1])


def _name(path: str):  # noqa: ANN202
    return lambda ctx: Coalesce(F(path), Value(""))  # noqa: E731


# ------------------------------------------------------------------------------------------------------------------------------ executions
def build_executions(ctx: Ctx):  # noqa: ANN202
    qs = execution_selectors.executions_visible_to(ctx.user).filter(organization=ctx.organization)
    qs = apply_common(ctx, qs, date_field="started_at", location_path="assignment__location_id")
    return build_list(ctx, qs)


register(Report(
    code="executions", version=1, title="Checklist executions", category="Checklists", kind="list", build=build_executions, date_field="started_at", date_label="the execution start time",
    description="Every checklist execution with its exact published version, status, timing, and the stored score and evaluation outcome where the user may see them.",
    columns=(C("number", "Execution", INT, "number"), C("template", "Checklist", TEXT, "version__template__name"), C("version", "Version", INT, "version__version_number"),
             C("assignment", "Assignment", INT, "assignment__number"), C("location", "Location", TEXT, "assignment__location__name"), C("status", "Status", TEXT, "status"),
             C("started_at", "Started", DATETIME, "started_at"), C("submitted_at", "Submitted", DATETIME, "submitted_at"),
             C("duration", "Duration (min)", DURATION, lambda ctx: ExpressionWrapper(F("submitted_at") - F("started_at"), output_field=DurationField())),
             C("score_percentage", "Score %", DECIMAL, _score_sub("percentage")), C("grade", "Grade", TEXT, _score_sub("grade")),
             C("evaluation", "Evaluation", TEXT, _eval_sub), C("started_by", "Started by", TEXT, _name("started_by__display_name"), people=True, default=False),
             C("assigned_to", "Assigned to", TEXT, _name("assignment__assigned_to__display_name"), people=True, default=False)),
    filters=(LOCATION, DESCENDANTS, TEMPLATE("version__template_id"), F_("version", "uuid", "Published version", "version_id"), F_("status", "choice", "Status", "status", STATUS_EXEC),
             F_("assigned_to", "uuid", "Assigned to", "assignment__assigned_to_id", people=True)),
    location_path="assignment__location_id", default_sort="-started_at", source_permission=P.EXECUTION_VIEW,
    notes="Answers and evidence are not part of this report. Score and evaluation are the stored current snapshots (nothing is recalculated); a blank means none exists or you may not view it, never zero."))


# ------------------------------------------------------------------------------------------------------------------------------ assignments
def _assign_overdue(ctx: Ctx):  # noqa: ANN202
    return Case(When(status__in=[s.value for s in ASSIGNMENT_OPEN], due_at__lt=ctx.now, then=Value(True)), default=Value(False))


def build_assignments(ctx: Ctx):  # noqa: ANN202
    qs = assignment_selectors.assignments_visible_to(ctx.user, ctx.organization)
    qs = apply_common(ctx, qs, date_field="created_at")
    if ctx.params.filters.get("overdue") is True:
        qs = qs.filter(status__in=[s.value for s in ASSIGNMENT_OPEN], due_at__lt=ctx.now)
    return build_list(ctx, qs)


register(Report(
    code="assignments", version=1, title="Assignments and overdue work", category="Checklists", kind="list", build=build_assignments, date_field="created_at", date_label="the assignment creation time",
    description="Checklist assignments with status, priority, due date and the derived overdue indicator (open work whose due date has passed; cancelled, completed and draft assignments are never overdue).",
    columns=(C("number", "Assignment", INT, "number"), C("template", "Checklist", TEXT, "version__template__name"), C("version", "Version", INT, "version__version_number"),
             C("location", "Location", TEXT, "location__name"), C("status", "Status", TEXT, "status"), C("priority", "Priority", TEXT, "priority"), C("issued_at", "Issued", DATETIME, "issued_at"),
             C("due_at", "Due", DATETIME, "due_at"), C("overdue", "Overdue", BOOL, _assign_overdue), C("created_at", "Created", DATETIME, "created_at", default=False),
             C("assigned_to", "Assigned to", TEXT, _name("assigned_to__display_name"), people=True)),
    filters=(LOCATION, DESCENDANTS, TEMPLATE("version__template_id"), F_("status", "choice", "Status", "status", STATUS_ASSIGNMENT), F_("priority", "choice", "Priority", "priority", PRIORITY_ASSIGNMENT),
             F_("overdue", "bool", "Overdue only"), ASSIGNEE("assigned_to_id")),
    default_sort="-created_at", source_permission=P.ASSIGNMENT_VIEW))


# ------------------------------------------------------------------------------------------------------------------------------ findings
def _action_count(open_only: bool):  # noqa: ANN202
    def build(ctx: Ctx):  # noqa: ANN202
        qs = CorrectiveAction.objects.filter(finding=OuterRef("pk"))
        if open_only:
            qs = qs.exclude(status__in=[ActionStatus.CLOSED, ActionStatus.CANCELLED])
        return Coalesce(Subquery(qs.order_by().values("finding").annotate(c=Count("pk")).values("c")), 0, output_field=IntegerField())
    return build


def build_findings(ctx: Ctx):  # noqa: ANN202
    qs = finding_selectors.findings_visible_to(ctx.user, ctx.organization)
    return build_list(ctx, apply_common(ctx, qs, date_field="created_at"))


register(Report(
    code="findings", version=1, title="Findings", category="Quality", kind="list", build=build_findings, date_field="created_at", date_label="the finding creation time",
    description="Findings with category, severity, status, location, source execution, due date and lifecycle timestamps. Closure is the finding's own status - never inferred from corrective actions.",
    columns=(C("number", "Finding", INT, "number"), C("title", "Title", TEXT, "title"), C("category", "Category", TEXT, "category__name"), C("severity", "Severity", TEXT, "severity__name"),
             C("severity_rank", "Severity rank", INT, "severity__rank", default=False), C("status", "Status", TEXT, "status"), C("location", "Location", TEXT, "location__name"),
             C("execution", "Execution", INT, "execution__number"), C("created_at", "Created", DATETIME, "created_at"), C("due_at", "Due", DATETIME, "due_at"), C("resolved_at", "Resolved", DATETIME, "resolved_at"),
             C("closed_at", "Closed", DATETIME, "closed_at"), C("actions", "Corrective actions", INT, _action_count(False)), C("open_actions", "Open actions", INT, _action_count(True), default=False),
             C("assigned_to", "Owner", TEXT, _name("assigned_to__display_name"), people=True)),
    filters=(LOCATION, DESCENDANTS, F_("status", "choice", "Status", "status", STATUS_FINDING), F_("severity", "uuid", "Severity", "severity_id"), F_("category", "uuid", "Category", "category_id"),
             ASSIGNEE("assigned_to_id")),
    default_sort="-created_at", source_permission=P.FINDING_VIEW))


# ------------------------------------------------------------------------------------------------------------------------------ corrective actions
def _action_overdue(ctx: Ctx):  # noqa: ANN202
    return Case(When(status__in=[s.value for s in WORKING_STATUSES], due_at__lt=ctx.now, then=Value(True)), default=Value(False))


def build_actions(ctx: Ctx):  # noqa: ANN202
    qs = action_selectors.actions_visible_to(ctx.user, ctx.organization)
    qs = apply_common(ctx, qs, date_field="created_at")
    if ctx.params.filters.get("overdue") is True:
        qs = qs.filter(status__in=[s.value for s in WORKING_STATUSES], due_at__lt=ctx.now)
    if ctx.params.filters.get("verified") is True:
        qs = qs.filter(verified_at__isnull=False)
    return build_list(ctx, qs)


register(Report(
    code="corrective_actions", version=1, title="Corrective actions", category="Quality", kind="list", build=build_actions, date_field="created_at", date_label="the action creation time",
    description="Corrective actions with finding, status, priority, owner, due date, owner submission and independent verification dates. Overdue means the owner still has work to do and the due date has passed.",
    columns=(C("number", "Action", INT, "number"), C("title", "Title", TEXT, "title"), C("finding", "Finding", INT, "finding__number"), C("status", "Status", TEXT, "status"),
             C("priority", "Priority", TEXT, "priority"), C("location", "Location", TEXT, "location__name"), C("due_at", "Due", DATETIME, "due_at"), C("submitted_at", "Submitted by owner", DATETIME, "submitted_at"),
             C("verified_at", "Verified", DATETIME, "verified_at"), C("overdue", "Overdue", BOOL, _action_overdue), C("closed_at", "Closed", DATETIME, "closed_at", default=False),
             C("created_at", "Created", DATETIME, "created_at", default=False), C("assigned_to", "Owner", TEXT, _name("assigned_to__display_name"), people=True)),
    filters=(LOCATION, DESCENDANTS, F_("status", "choice", "Status", "status", STATUS_ACTION), F_("priority", "choice", "Priority", "priority", PRIORITY_ACTION), F_("overdue", "bool", "Overdue only"),
             F_("verified", "bool", "Verified only"), ASSIGNEE("assigned_to_id")),
    default_sort="-created_at", source_permission=P.CORRECTIVE_ACTION_VIEW,
    notes="Owner submission is not verification; verification is not closure of the finding. A status of 'verified' awaits closure."))


# ------------------------------------------------------------------------------------------------------------------------------ workflows
def _open_tasks(ctx: Ctx):  # noqa: ANN202
    return Coalesce(Subquery(WorkflowTask.objects.filter(instance=OuterRef("pk"), status__in=["pending", "claimed"]).order_by().values("instance").annotate(c=Count("pk")).values("c")), 0,
                    output_field=IntegerField())


def _waiting_on(ctx: Ctx):  # noqa: ANN202
    return Coalesce(Subquery(WorkflowTask.objects.filter(instance=OuterRef("pk"), status__in=["pending", "claimed"], assignee__isnull=False).order_by("due_at", "pk").values("assignee__display_name")[:1]),
                    Value(""), output_field=CharField())


def _hours_in_state(ctx: Ctx):  # noqa: ANN202
    return Case(When(status=InstanceStatus.ACTIVE, then=ExpressionWrapper(Value(ctx.now) - F("state_entered_at"), output_field=DurationField())), default=None, output_field=DurationField())


def build_workflows(ctx: Ctx):  # noqa: ANN202
    qs = workflow_selectors.instances_visible_to(ctx.user).filter(organization=ctx.organization)
    return build_list(ctx, apply_common(ctx, qs, date_field="started_at"))


register(Report(
    code="workflows", version=1, title="Workflows and approvals", category="Quality", kind="list", build=build_workflows, date_field="started_at", date_label="the workflow start time",
    description="Workflow instances with definition, status, current state, open tasks and (with people access) who they are waiting on. Time in the current state is shown for active instances only.",
    columns=(C("number", "Workflow", INT, "number"), C("definition", "Definition", TEXT, "definition__name"), C("status", "Status", TEXT, "status"), C("outcome", "Outcome", TEXT, "outcome"),
             C("state", "Current state", TEXT, "state__name"), C("target", "Target", TEXT, "target_type"), C("location", "Location", TEXT, "location__name"), C("started_at", "Started", DATETIME, "started_at"),
             C("completed_at", "Completed", DATETIME, "completed_at"), C("open_tasks", "Open tasks", INT, _open_tasks), C("time_in_state", "In state (min)", DURATION, _hours_in_state),
             C("waiting_on", "Waiting on", TEXT, _waiting_on, people=True)),
    filters=(LOCATION, DESCENDANTS, F_("status", "choice", "Status", "status", STATUS_INSTANCE), F_("definition", "uuid", "Workflow definition", "definition_id")),
    default_sort="-started_at", source_permission=P.WORKFLOW_INSTANCE_VIEW,
    notes="Only recorded data is reported: no transition history beyond what the instance stores, no inferred approvals."))


# ------------------------------------------------------------------------------------------------------------------------------ evaluations
def build_evaluations(ctx: Ctx):  # noqa: ANN202
    qs = evaluation_selectors.runs_visible_to(ctx.user).filter(organization=ctx.organization)
    qs = apply_common(ctx, qs, date_field="created_at", location_path="execution__assignment__location_id", permission=P.REPORT_VIEW)
    if ctx.params.filters.get("current") is True:
        qs = qs.filter(is_current=True)
    return build_list(ctx, qs)


register(Report(
    code="evaluations", version=1, title="Automated evaluation outcomes", category="Quality", kind="list", build=build_evaluations, date_field="created_at", date_label="the evaluation time",
    description="Stored evaluation runs (Phase 16): outcome, rule-set version, trigger and whether it is the current one. Re-evaluations are separate rows; nothing is re-run to produce this report.",
    columns=(C("execution", "Execution", INT, "execution__number"), C("template", "Checklist", TEXT, "version__template__name"), C("location", "Location", TEXT, "execution__assignment__location__name"),
             C("outcome", "Outcome", TEXT, "outcome"), C("current", "Current", BOOL, "is_current"), C("trigger", "Trigger", TEXT, "trigger"), C("created_at", "Evaluated", DATETIME, "created_at"),
             C("ruleset_version", "Version", INT, "version__version_number", default=False), C("reason", "Reason", TEXT, "reason", default=False)),
    filters=(LOCATION, DESCENDANTS, TEMPLATE("version__template_id"), F_("outcome", "choice", "Outcome", "outcome", OUTCOMES), F_("current", "bool", "Current evaluation only")),
    location_path="execution__assignment__location_id", default_sort="-created_at", source_permission=P.EVALUATION_RESULT_VIEW))


# ------------------------------------------------------------------------------------------------------------------------------ scores (list)
def build_scores(ctx: Ctx):  # noqa: ANN202
    qs = scoring_selectors.results_visible_to(ctx.user).filter(organization=ctx.organization, is_current=True, status=ResultStatus.FINAL)
    qs = apply_common(ctx, qs, date_field="execution__submitted_at", location_path="execution__assignment__location_id", permission=P.SCORING_SCORE_VIEW)
    return build_list(ctx, qs)


register(Report(
    code="scores", version=1, title="Checklist scores", category="Scoring", kind="list", build=build_scores, date_field="execution__submitted_at", date_label="the execution submission time",
    description="The stored current final score of submitted executions (Phase 15), under the exact version used. Executions without a final score are not listed - a missing score is never treated as zero.",
    columns=(C("execution", "Execution", INT, "execution__number"), C("template", "Checklist", TEXT, "version__template__name"), C("version", "Version", INT, "version__version_number"),
             C("location", "Location", TEXT, "execution__assignment__location__name"), C("submitted_at", "Submitted", DATETIME, "execution__submitted_at"), C("earned", "Earned", DECIMAL, "earned"),
             C("available", "Available", DECIMAL, "available"), C("percentage", "Score %", DECIMAL, "percentage"), C("grade", "Grade", TEXT, "grade"), C("critical", "Critical failure", BOOL, "critical"),
             C("kind", "Snapshot", TEXT, "kind", default=False)),
    filters=(LOCATION, DESCENDANTS, TEMPLATE("version__template_id"), F_("version", "uuid", "Published version", "version_id")),
    location_path="execution__assignment__location_id", default_sort="-submitted_at", source_permission=P.SCORING_SCORE_VIEW,
    notes="'Snapshot' tells whether the score was calculated at submission or by an authorized recalculation."))



# ================================================================================================================================ summaries
def _group(ctx: Ctx, field_by_group: dict[str, tuple[str, Any]]):  # noqa: ANN202
    """``(label alias, expression)`` of the requested grouping, or ``None`` for the overall total."""
    return field_by_group.get(ctx.params.group_by)


def _pct(part: int, whole: int) -> float | None:
    return round(part * 100 / whole, 1) if whole else None


COMPLETION_GROUPS = (("location", "Location"), ("template", "Checklist"), ("day", "Day issued"), ("week", "Week issued"), ("month", "Month issued"), ("quarter", "Quarter issued"))


def build_completion(ctx: Ctx) -> list[dict[str, Any]]:
    """Denominator: assignments that were *issued* in the period and are not cancelled (draft assignments were never issued).  Each assignment counts once - executions are
    counted separately, so retries never inflate the totals.  Overdue = open (assigned / in progress) with a past due date.  Empty time buckets are listed with zero counts."""
    from apps.executions.models import ChecklistExecution

    from .summaries import summarize

    base = assignment_selectors.assignments_visible_to(ctx.user, ctx.organization).exclude(status__in=[AssignmentStatus.DRAFT, AssignmentStatus.CANCELLED])
    base = apply_common(ctx, base, date_field="issued_at")
    open_values = [s.value for s in ASSIGNMENT_OPEN]
    rows = summarize(ctx, period_qs=base, date_path="issued_at", groups={"location": ("location_id", "location__name", None), "template": ("version__template_id", "version__template__name", None)},
                     period_metrics={"eligible": Count("pk"), "completed": Count("pk", filter=Q(status=AssignmentStatus.COMPLETED)),
                                     "in_progress": Count("pk", filter=Q(status=AssignmentStatus.IN_PROGRESS)), "pending": Count("pk", filter=Q(status=AssignmentStatus.ASSIGNED)),
                                     "overdue": Count("pk", filter=Q(status__in=open_values, due_at__lt=ctx.now))})
    executions = ChecklistExecution.objects.filter(assignment__in=base.values("pk"), status=ExecutionStatus.SUBMITTED, submitted_at__isnull=False, started_at__isnull=False)
    durations = {r["key"]: r["avg_minutes"] for r in summarize(
        ctx, period_qs=executions, date_path="assignment__issued_at", averages=("avg_minutes",),
        groups={"location": ("assignment__location_id", "assignment__location__name", None), "template": ("version__template_id", "version__template__name", None)},
        period_metrics={"avg_minutes": Avg(ExpressionWrapper(F("submitted_at") - F("started_at"), output_field=DurationField()))})}
    for row in rows:
        row["completion_pct"] = _pct(row["completed"], row["eligible"])
        row["avg_minutes"] = durations.get(row["key"])
    return rows


register(Report(
    code="completion", version=1, title="Checklist completion", category="Checklists", kind="summary", build=build_completion, date_field="issued_at", date_label="the assignment issue time",
    description="Completion of assignments issued in the period: eligible, completed, in progress, not started, overdue, completion % and the average submitted-execution duration.",
    columns=(C("group", "Group", TEXT), C("eligible", "Eligible", INT), C("completed", "Completed", INT), C("in_progress", "In progress", INT), C("pending", "Not started", INT), C("overdue", "Overdue", INT),
             C("completion_pct", "Completion %", DECIMAL), C("avg_minutes", "Avg duration (min)", DECIMAL)),
    filters=(LOCATION, DESCENDANTS, TEMPLATE("version__template_id")), group_by=COMPLETION_GROUPS, default_sort="group", source_permission=P.ASSIGNMENT_VIEW,
    notes="Eligible = issued and not cancelled. Completion % = completed / eligible. Rows without eligible assignments show no percentage."))

SCORE_GROUPS = (("template", "Checklist"), ("location", "Location"), ("month", "Month submitted"))


def build_score_summary(ctx: Ctx) -> list[dict[str, Any]]:
    qs = scoring_selectors.results_visible_to(ctx.user).filter(organization=ctx.organization, is_current=True, status=ResultStatus.FINAL)
    qs = apply_common(ctx, qs, date_field="execution__submitted_at", location_path="execution__assignment__location_id", permission=P.SCORING_SCORE_VIEW)
    groups = {"template": (F("version__template_id"), F("version__template__name")), "location": (F("execution__assignment__location_id"), F("execution__assignment__location__name")),
              "month": (TruncMonth("execution__submitted_at", tzinfo=timezone.get_default_timezone()), Value(""))}
    key, label = groups.get(ctx.params.group_by, (Value("all"), Value("All")))
    rows = (qs.annotate(gk=key, g=label).order_by().values("gk", "g").annotate(scored=Count("pk"), average=Avg("percentage"), lowest=Min("percentage"), highest=Max("percentage"),
                                                                              critical=Count("pk", filter=Q(critical=True))).order_by("gk")[: k.MAX_SUMMARY_GROUPS])
    out = []
    for r in rows:
        month = hasattr(r["gk"], "strftime")
        out.append({"group": timezone.localtime(r["gk"]).strftime("%Y-%m") if month else r["g"], "key": timezone.localtime(r["gk"]).date().isoformat() if month else str(r["gk"]),
                    "scored": r["scored"], "average": round(float(r["average"]), 2) if r["average"] is not None else None, "lowest": r["lowest"], "highest": r["highest"], "critical": r["critical"]})
    return out


register(Report(
    code="score_summary", version=1, title="Score summary", category="Scoring", kind="summary", build=build_score_summary, date_field="execution__submitted_at", date_label="the execution submission time",
    description="Average, lowest and highest stored final score percentage and critical-failure count of submitted executions, grouped by checklist, location or month. Unscored executions are excluded, never counted as zero.",
    columns=(C("group", "Group", TEXT), C("scored", "Scored executions", INT), C("average", "Average %", DECIMAL), C("lowest", "Lowest %", DECIMAL), C("highest", "Highest %", DECIMAL), C("critical", "Critical failures", INT)),
    filters=(LOCATION, DESCENDANTS, TEMPLATE("version__template_id")), group_by=SCORE_GROUPS, default_sort="group", source_permission=P.SCORING_SCORE_VIEW))


def build_comparison(ctx: Ctx) -> list[dict[str, Any]]:
    """One row per authorized location with *consistently defined* metrics.  Period metrics use the date range; "now" metrics are the state at generation time (and say so)."""
    from apps.executions.models import ChecklistExecution

    locations = ctx.locations_for().filter(organization=ctx.organization)
    ids = ctx.location_filter_ids()
    if ids is not None:
        locations = locations.filter(pk__in=ids)
    loc_rows = list(locations.order_by("name", "pk").values("pk", "name")[: k.MAX_SUMMARY_GROUPS])
    allowed = [r["pk"] for r in loc_rows]
    open_values = [s.value for s in ASSIGNMENT_OPEN]

    def by_location(qs, path, **aggregates):  # noqa: ANN001, ANN202
        data: dict[Any, dict[str, Any]] = defaultdict(dict)
        for row in qs.filter(**{f"{path}__in": allowed}).order_by().values(path).annotate(**aggregates):  # order_by(): a default Meta ordering would otherwise join the GROUP BY
            data[row[path]] = row
        return data

    a_qs = assignment_selectors.assignments_visible_to(ctx.user, ctx.organization).exclude(status__in=[AssignmentStatus.DRAFT, AssignmentStatus.CANCELLED]).filter(issued_at__gte=ctx.params.start, issued_at__lt=ctx.params.end)
    assignments = by_location(a_qs, "location_id", eligible=Count("pk"), completed=Count("pk", filter=Q(status=AssignmentStatus.COMPLETED)), overdue=Count("pk", filter=Q(status__in=open_values, due_at__lt=ctx.now)))
    e_qs = execution_selectors.executions_visible_to(ctx.user).filter(organization=ctx.organization, status=ExecutionStatus.SUBMITTED, submitted_at__gte=ctx.params.start, submitted_at__lt=ctx.params.end)
    executions = by_location(e_qs, "assignment__location_id", submitted=Count("pk", distinct=True))
    s_qs = scoring_selectors.results_visible_to(ctx.user).filter(organization=ctx.organization, is_current=True, status=ResultStatus.FINAL, execution__submitted_at__gte=ctx.params.start, execution__submitted_at__lt=ctx.params.end,
                                                                  execution__assignment__location__in=ctx.locations_for(P.SCORING_SCORE_VIEW).values("pk"))
    scores = by_location(s_qs, "execution__assignment__location_id", average=Avg("percentage"))
    f_qs = finding_selectors.findings_visible_to(ctx.user, ctx.organization).filter(status__in=[s.value for s in FINDING_ACTIVE])
    findings = by_location(f_qs, "location_id", n=Count("pk"))
    c_qs = action_selectors.actions_visible_to(ctx.user, ctx.organization).filter(status__in=[s.value for s in WORKING_STATUSES] + [ActionStatus.SUBMITTED, ActionStatus.VERIFIED])
    actions = by_location(c_qs, "location_id", open=Count("pk"), overdue=Count("pk", filter=Q(status__in=[s.value for s in WORKING_STATUSES], due_at__lt=ctx.now)))
    w_qs = workflow_selectors.instances_visible_to(ctx.user).filter(organization=ctx.organization, status=InstanceStatus.ACTIVE)
    workflows = by_location(w_qs, "location_id", n=Count("pk"))
    out = []
    for loc in loc_rows:
        pk = loc["pk"]
        a, s = assignments.get(pk, {}), scores.get(pk, {})
        out.append({"location": loc["name"], "eligible": a.get("eligible", 0), "completed": a.get("completed", 0), "completion_pct": _pct(a.get("completed", 0), a.get("eligible", 0)), "overdue": a.get("overdue", 0),
                    "submitted": executions.get(pk, {}).get("submitted", 0), "avg_score": round(float(s["average"]), 2) if s.get("average") is not None else None,
                    "open_findings": findings.get(pk, {}).get("n", 0), "open_actions": actions.get(pk, {}).get("open", 0), "overdue_actions": actions.get(pk, {}).get("overdue", 0),
                    "workflow_backlog": workflows.get(pk, {}).get("n", 0)})
    return out


register(Report(
    code="location_comparison", version=1, title="Location comparison", category="Comparison", kind="summary", build=build_comparison, date_field="", date_label="issued / submitted within the period",
    description="One row per location you may report on. Period metrics (assignments issued, executions submitted, average stored score) use the date range; 'now' metrics (open findings, open and overdue actions, active workflows) are the state when generated. Each location is counted on its own: sub-locations are not rolled up.",
    columns=(C("location", "Location", TEXT), C("eligible", "Assignments (period)", INT), C("completed", "Completed", INT), C("completion_pct", "Completion %", DECIMAL), C("overdue", "Overdue assignments", INT),
             C("submitted", "Executions submitted", INT), C("avg_score", "Average score %", DECIMAL), C("open_findings", "Open findings (now)", INT), C("open_actions", "Open actions (now)", INT),
             C("overdue_actions", "Overdue actions (now)", INT), C("workflow_backlog", "Active workflows (now)", INT)),
    filters=(LOCATION, DESCENDANTS), default_sort="location", source_permission=P.ASSIGNMENT_VIEW,
    notes="Counts of findings, actions and workflows are limited to what you may view in each module. The location hierarchy and types are the organization's own; nothing is assumed about their names."))


from . import summaries  # noqa: E402,F401  (registers the summary reports used by dashboards - Phase 20)
