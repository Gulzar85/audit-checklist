"""The fixed registries of the notifications engine: **source types**, **event types** and the built-in default rules and templates.

Nothing here (or in a rule) can name a Django model, a database field or code.  A *source type* says how to load an object of one allowlisted kind, which
organization / location it belongs to, who may open it (the owning module's own policy) and its safe internal link.  An *event type* says which source
it is about, which facts a rule filter may use, which variables a template may use, and its built-in default rule and template.  Adding an event is
deliberate: register it here and publish it from the owning module's existing signal in ``integration.py``.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from django.conf import settings

from apps.checklists.rules.operators import CHOICE, ORDERED, TEXT  # noqa: F401  (comparison families: the Phase 6 vocabulary)
from apps.core.utils import parse_uuid

from . import constants as k


# ---------------------------------------------------------------------------------------------------------------------------------
# Source types
# ---------------------------------------------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class Source:
    type: str
    label: str
    load: Callable[[Any], Any]
    organization: Callable[[Any], Any]
    location: Callable[[Any], Any]
    can_view: Callable[[Any, Any], bool]
    reference: Callable[[Any], str]
    title: Callable[[Any], str]
    url: Callable[[Any], str]
    assignee_id: Callable[[Any], Any] = lambda obj: None
    creator_id: Callable[[Any], Any] = lambda obj: None


def _by_pk(model_path: str, *related: str) -> Callable[[Any], Any]:
    def load(pk: Any) -> Any:
        from django.apps import apps

        parsed = parse_uuid(pk)
        if parsed is None:
            return None
        qs = apps.get_model(model_path).objects.all()
        return (qs.select_related(*related) if related else qs).filter(pk=parsed).first()

    return load


def _assignment_can_view(user: Any, obj: Any) -> bool:
    from apps.assignments import policies

    return policies.can_view_assignment(user, obj)


def _execution_can_view(user: Any, obj: Any) -> bool:
    from apps.executions import policies

    return policies.can_view(user, obj)


def _finding_can_view(user: Any, obj: Any) -> bool:
    from apps.findings import policies

    return policies.can_view(user, obj)


def _action_can_view(user: Any, obj: Any) -> bool:
    from apps.corrective_actions import policies

    return policies.can_view(user, obj)


def _task_can_view(user: Any, obj: Any) -> bool:
    from apps.workflows import policies

    return obj.assignee_id == getattr(user, "pk", None) or policies.can_view_instance(user, obj.instance, has_task=True)


def _instance_can_view(user: Any, obj: Any) -> bool:
    from apps.workflows import policies

    return policies.can_view_instance(user, obj)


def _evaluation_can_view(user: Any, obj: Any) -> bool:
    from apps.evaluation import policies

    return policies.can_view_result(user, obj.execution)


def _delivery_can_view(user: Any, obj: Any) -> bool:
    from . import policies

    return policies.can_view_deliveries(user, obj.organization)


def _delivery_url(obj: Any) -> str:
    from django.urls import reverse

    return reverse("notifications:deliveries")


def _evaluation_url(obj: Any) -> str:
    from django.urls import reverse

    return reverse("evaluation:execution", args=[obj.execution_id])


SOURCES: dict[str, Source] = {s.type: s for s in (
    Source("assignment", "Checklist assignment", _by_pk("assignments.ChecklistAssignment", "organization", "location", "version__template"), lambda o: o.organization, lambda o: o.location,
           _assignment_can_view, lambda o: o.reference, lambda o: o.version.template.name, lambda o: o.get_absolute_url(), lambda o: o.assigned_to_id, lambda o: o.created_by_id),
    Source("execution", "Checklist execution", _by_pk("executions.ChecklistExecution", "organization", "assignment__location", "version__template"), lambda o: o.organization,
           lambda o: o.assignment.location, _execution_can_view, lambda o: o.reference, lambda o: o.version.template.name, lambda o: o.get_absolute_url(),
           lambda o: o.assignment.assigned_to_id, lambda o: o.assignment.created_by_id),
    Source("finding", "Finding", _by_pk("findings.Finding", "organization", "location", "severity", "category"), lambda o: o.organization, lambda o: o.location, _finding_can_view,
           lambda o: o.reference, lambda o: o.title, lambda o: o.get_absolute_url(), lambda o: o.assigned_to_id, lambda o: o.detected_by_id),
    Source("corrective_action", "Corrective action", _by_pk("corrective_actions.CorrectiveAction", "organization", "location", "finding"), lambda o: o.organization, lambda o: o.location,
           _action_can_view, lambda o: o.reference, lambda o: o.title, lambda o: o.get_absolute_url(), lambda o: o.assigned_to_id, lambda o: o.created_by_id),
    Source("workflow_task", "Workflow task", _by_pk("workflows.WorkflowTask", "instance__organization", "instance__location", "instance__definition", "state", "assignee"),
           lambda o: o.instance.organization, lambda o: o.instance.location, _task_can_view, lambda o: o.instance.reference, lambda o: o.instance.definition.name,
           lambda o: o.instance.get_absolute_url(), lambda o: o.assignee_id, lambda o: o.instance.started_by_id),
    Source("workflow_instance", "Workflow", _by_pk("workflows.WorkflowInstance", "organization", "location", "definition", "state"), lambda o: o.organization, lambda o: o.location,
           _instance_can_view, lambda o: o.reference, lambda o: o.definition.name, lambda o: o.get_absolute_url(), lambda o: None, lambda o: o.started_by_id),
    Source("evaluation_run", "Automated evaluation", _by_pk("evaluation.EvaluationRun", "organization", "execution__assignment__location", "execution__version__template"),
           lambda o: o.organization, lambda o: o.execution.assignment.location, _evaluation_can_view, lambda o: o.execution.reference, lambda o: o.execution.version.template.name,
           _evaluation_url, lambda o: None, lambda o: o.created_by_id),
    Source("notification_delivery", "Notification delivery", _by_pk("notifications.NotificationDelivery", "organization"), lambda o: o.organization, lambda o: None, _delivery_can_view,
           lambda o: "Delivery", lambda o: f"{o.get_channel_display()} notification", _delivery_url),
)}


def get_source(source_type: str) -> Source | None:
    return SOURCES.get(str(source_type))


# ---------------------------------------------------------------------------------------------------------------------------------
# Event types
# ---------------------------------------------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class Fact:
    """An attribute a rule filter may compare (a Phase 6 comparison family decides the operators)."""

    code: str
    label: str
    family: str
    kind: str = "str"  # str | int | bool
    choices: tuple[str, ...] | None = None


@dataclass(frozen=True)
class DefaultRule:
    strategies: tuple[dict[str, Any], ...]
    channels: tuple[str, ...] = (k.Channel.IN_APP, k.Channel.EMAIL)
    priority: str = k.Priority.NORMAL
    policy: str = k.PreferencePolicy.OPERATIONAL
    filters: tuple[dict[str, Any], ...] = ()
    exclude_actor: bool = True


@dataclass(frozen=True)
class Event:
    code: str
    label: str
    category: str
    source_type: str
    description: str
    default_rule: DefaultRule
    subject: str
    body: str
    title: str
    message: str
    facts: tuple[Fact, ...] = ()
    extra_variables: tuple[str, ...] = ()
    facts_of: Callable[[Any, dict[str, Any]], dict[str, Any]] = lambda source, meta: {}
    context_of: Callable[[Any, dict[str, Any]], dict[str, str]] = lambda source, meta: {}

    @property
    def variables(self) -> tuple[str, ...]:
        return COMMON_VARIABLES + self.extra_variables


COMMON_VARIABLES = ("recipient_name", "organization_name", "reference", "title", "location_name", "actor_name", "link", "event_label")


def _s(value: Any, limit: int = 160) -> str:
    return " ".join(str(value if value is not None else "").split())[:limit]


def _date(value: Any) -> str:
    return value.strftime("%d %b %Y %H:%M") if value else "not set"


def _ca_facts(source: Any, meta: dict[str, Any]) -> dict[str, Any]:
    return {"priority": source.priority, "overdue": bool(source.is_overdue), "status": source.status}


def _ca_context(source: Any, meta: dict[str, Any]) -> dict[str, str]:
    return {"due_date": _date(source.due_at), "priority": _s(source.get_priority_display()), "status": _s(source.get_status_display())}


def _finding_facts(source: Any, meta: dict[str, Any]) -> dict[str, Any]:
    return {"severity": source.severity.code, "severity_rank": source.severity.rank, "category": source.category.code, "source_type": source.source_type, "needs_review": source.needs_review}


def _finding_context(source: Any, meta: dict[str, Any]) -> dict[str, str]:
    return {"severity": _s(source.severity.name), "category": _s(source.category.name), "status": _s(source.get_status_display())}


def _assignment_facts(source: Any, meta: dict[str, Any]) -> dict[str, Any]:
    return {"priority": source.priority}


def _assignment_context(source: Any, meta: dict[str, Any]) -> dict[str, str]:
    return {"due_date": _date(source.due_at), "priority": _s(source.get_priority_display())}


def _task_context(source: Any, meta: dict[str, Any]) -> dict[str, str]:
    return {"step": _s(source.state.name), "due_date": _date(source.due_at)}


def _instance_facts(source: Any, meta: dict[str, Any]) -> dict[str, Any]:
    return {"outcome": source.outcome or ""}


def _instance_context(source: Any, meta: dict[str, Any]) -> dict[str, str]:
    return {"outcome": _s(source.outcome or source.status).replace("_", " ")}


def _evaluation_facts(source: Any, meta: dict[str, Any]) -> dict[str, Any]:
    return {"outcome": source.outcome}


def _evaluation_context(source: Any, meta: dict[str, Any]) -> dict[str, str]:
    return {"outcome": _s(source.get_outcome_display())}


def _delivery_context(source: Any, meta: dict[str, Any]) -> dict[str, str]:
    return {"channel": _s(source.get_channel_display()), "error": _s(meta.get("error_category", "")).replace("_", " ")}


_PRIORITY = Fact("priority", "Priority", CHOICE, "str", ("low", "normal", "high", "urgent"))
_OUTCOME = Fact("outcome", "Outcome", CHOICE, "str")
_SEVERITY = (Fact("severity", "Severity (code)", CHOICE), Fact("severity_rank", "Severity rank", ORDERED, "int"), Fact("category", "Category (code)", CHOICE),
             Fact("needs_review", "Needs review", "equality", "bool"))
_ASSIGNEE = ({"type": k.Strategy.ASSIGNEE},)

EVENTS: dict[str, Event] = {e.code: e for e in (
    Event("assignment.issued", "Checklist assigned", k.Category.ASSIGNMENTS, "assignment", "A checklist assignment was issued to someone.", DefaultRule(_ASSIGNEE),
          "Checklist assigned: {{ title }}", "{{ actor_name }} assigned you the checklist “{{ title }}” ({{ reference }}) at {{ location_name }}. Due: {{ due_date }}.\n\nOpen it: {{ link }}",
          "Checklist assigned", "{{ title }} at {{ location_name }}. Due: {{ due_date }}.", (Fact("priority", "Priority", CHOICE, "str", ("low", "normal", "high", "urgent")),),
          ("due_date", "priority"), _assignment_facts, _assignment_context),
    Event("assignment.assignee_changed", "Checklist reassigned", k.Category.ASSIGNMENTS, "assignment", "The person a checklist is assigned to changed.", DefaultRule(_ASSIGNEE),
          "Checklist assigned to you: {{ title }}", "The checklist “{{ title }}” ({{ reference }}) at {{ location_name }} is now assigned to you. Due: {{ due_date }}.\n\nOpen it: {{ link }}",
          "Checklist assigned to you", "{{ title }} at {{ location_name }}. Due: {{ due_date }}.", (Fact("priority", "Priority", CHOICE, "str", ("low", "normal", "high", "urgent")),),
          ("due_date", "priority"), _assignment_facts, _assignment_context),
    Event("assignment.cancelled", "Checklist assignment cancelled", k.Category.ASSIGNMENTS, "assignment", "A checklist assignment was cancelled.", DefaultRule(_ASSIGNEE, policy=k.PreferencePolicy.OPTIONAL),
          "Checklist cancelled: {{ title }}", "The checklist “{{ title }}” ({{ reference }}) at {{ location_name }} was cancelled. You no longer need to complete it.",
          "Checklist cancelled", "{{ title }} at {{ location_name }} was cancelled.", (), ("due_date", "priority"), _assignment_facts, _assignment_context),
    Event("execution.submitted", "Checklist submitted", k.Category.EXECUTIONS, "execution", "A checklist execution was submitted.", DefaultRule(({"type": k.Strategy.CREATOR},), policy=k.PreferencePolicy.OPTIONAL),
          "Checklist submitted: {{ title }}", "{{ actor_name }} submitted the checklist “{{ title }}” ({{ reference }}) at {{ location_name }}.\n\nOpen it: {{ link }}",
          "Checklist submitted", "{{ actor_name }} submitted {{ title }} at {{ location_name }}.", (), ()),
    Event("finding.raised", "Finding raised", k.Category.FINDINGS, "finding", "A finding was raised (by a rule, an evaluation or a person).",
          DefaultRule(({"type": k.Strategy.ASSIGNEE}, {"type": k.Strategy.PERMISSION, "permission": "finding.acknowledge"})),
          "Finding raised: {{ title }}", "A {{ severity }} finding was raised at {{ location_name }}: “{{ title }}” ({{ reference }}).\n\nOpen it: {{ link }}",
          "Finding raised", "{{ severity }}: {{ title }} at {{ location_name }}.", _SEVERITY, ("severity", "category", "status"), _finding_facts, _finding_context),
    Event("finding.assigned", "Finding assigned", k.Category.FINDINGS, "finding", "A finding was assigned or reassigned.", DefaultRule(_ASSIGNEE),
          "Finding assigned to you: {{ title }}", "The {{ severity }} finding “{{ title }}” ({{ reference }}) at {{ location_name }} is assigned to you.\n\nOpen it: {{ link }}",
          "Finding assigned to you", "{{ severity }}: {{ title }} at {{ location_name }}.", _SEVERITY, ("severity", "category", "status"), _finding_facts, _finding_context),
    Event("corrective_action.assigned", "Corrective action assigned", k.Category.CORRECTIVE_ACTIONS, "corrective_action", "A corrective action was assigned or reassigned.", DefaultRule(_ASSIGNEE),
          "Action required: {{ title }}", "A corrective action assigned to you requires attention: “{{ title }}” ({{ reference }}) at {{ location_name }}. Due: {{ due_date }}.\n\nOpen it: {{ link }}",
          "Corrective action assigned", "{{ title }}. Due: {{ due_date }}.", (_PRIORITY, Fact("overdue", "Is overdue", "equality", "bool")), ("due_date", "priority", "status"), _ca_facts, _ca_context),
    Event("corrective_action.submitted", "Corrective action submitted for verification", k.Category.CORRECTIVE_ACTIONS, "corrective_action", "A corrective action was submitted for verification.",
          DefaultRule(({"type": k.Strategy.PERMISSION, "permission": "corrective_action.verify"},)),
          "Verification needed: {{ title }}", "The corrective action “{{ title }}” ({{ reference }}) at {{ location_name }} was submitted for verification.\n\nReview it: {{ link }}",
          "Verification needed", "{{ title }} at {{ location_name }} awaits verification.", (_PRIORITY,), ("due_date", "priority", "status"), _ca_facts, _ca_context),
    Event("corrective_action.rejected", "Corrective action rejected", k.Category.CORRECTIVE_ACTIONS, "corrective_action", "A verifier sent a corrective action back for correction.", DefaultRule(_ASSIGNEE),
          "Correction needed: {{ title }}", "The corrective action “{{ title }}” ({{ reference }}) was returned for correction.\n\nOpen it: {{ link }}",
          "Correction needed", "{{ title }} was returned for correction.", (_PRIORITY,), ("due_date", "priority", "status"), _ca_facts, _ca_context),
    Event("corrective_action.verified", "Corrective action verified", k.Category.CORRECTIVE_ACTIONS, "corrective_action", "A corrective action was independently verified.", DefaultRule(_ASSIGNEE, policy=k.PreferencePolicy.OPTIONAL),
          "Verified: {{ title }}", "The corrective action “{{ title }}” ({{ reference }}) was verified.", "Corrective action verified", "{{ title }} was verified.", (_PRIORITY,),
          ("due_date", "priority", "status"), _ca_facts, _ca_context),
    Event("corrective_action.closed", "Corrective action closed", k.Category.CORRECTIVE_ACTIONS, "corrective_action", "A corrective action was closed.", DefaultRule(_ASSIGNEE, policy=k.PreferencePolicy.OPTIONAL),
          "Closed: {{ title }}", "The corrective action “{{ title }}” ({{ reference }}) was closed.", "Corrective action closed", "{{ title }} was closed.", (_PRIORITY,),
          ("due_date", "priority", "status"), _ca_facts, _ca_context),
    Event("corrective_action.due_soon", "Corrective action due soon", k.Category.CORRECTIVE_ACTIONS, "corrective_action", "A corrective action is approaching its due date (emitted by an authorized process).",
          DefaultRule(_ASSIGNEE, priority=k.Priority.HIGH),
          "Due soon: {{ title }}", "The corrective action “{{ title }}” ({{ reference }}) is due {{ due_date }}.\n\nOpen it: {{ link }}", "Due soon", "{{ title }} is due {{ due_date }}.", (_PRIORITY,),
          ("due_date", "priority", "status"), _ca_facts, _ca_context),
    Event("corrective_action.overdue", "Corrective action overdue", k.Category.CORRECTIVE_ACTIONS, "corrective_action", "A corrective action is past its due date (emitted by an authorized process).",
          DefaultRule(_ASSIGNEE, priority=k.Priority.HIGH),
          "Overdue: {{ title }}", "The corrective action “{{ title }}” ({{ reference }}) was due {{ due_date }} and is overdue.\n\nOpen it: {{ link }}", "Overdue", "{{ title }} was due {{ due_date }}.", (_PRIORITY,),
          ("due_date", "priority", "status"), _ca_facts, _ca_context),
    Event("workflow.task_assigned", "Workflow task assigned", k.Category.WORKFLOWS, "workflow_task", "A workflow task needs someone's decision.", DefaultRule(({"type": k.Strategy.TASK_ASSIGNEES},)),
          "Approval needed: {{ title }}", "A task needs your attention: “{{ step }}” in {{ title }} ({{ reference }}) at {{ location_name }}. Due: {{ due_date }}.\n\nOpen it: {{ link }}",
          "Approval needed", "{{ step }} in {{ title }}. Due: {{ due_date }}.", (), ("step", "due_date"), lambda s, m: {}, _task_context),
    Event("workflow.completed", "Workflow completed", k.Category.WORKFLOWS, "workflow_instance", "A workflow finished (approved, rejected or cancelled).", DefaultRule(({"type": k.Strategy.CREATOR},), policy=k.PreferencePolicy.OPTIONAL),
          "Workflow {{ outcome }}: {{ title }}", "The workflow {{ title }} ({{ reference }}) finished: {{ outcome }}.\n\nOpen it: {{ link }}", "Workflow finished", "{{ title }}: {{ outcome }}.",
          (Fact("outcome", "Outcome", CHOICE),), ("outcome",), _instance_facts, _instance_context),
    Event("evaluation.recorded", "Evaluation recorded", k.Category.EVALUATIONS, "evaluation_run", "An automated evaluation of a submitted execution was recorded.",
          DefaultRule(({"type": k.Strategy.PERMISSION, "permission": "evaluation.result_view"},), priority=k.Priority.HIGH,
                      filters=({"attribute": "outcome", "operator": "CONTAINS_ANY", "value": ["review_required", "fail"]},)),
          "Evaluation needs attention: {{ title }}", "The automated evaluation of {{ reference }} ({{ title }}) at {{ location_name }} is {{ outcome }}.\n\nOpen it: {{ link }}",
          "Evaluation needs attention", "{{ reference }}: {{ outcome }}.", (_OUTCOME,), ("outcome",), _evaluation_facts, _evaluation_context),
    Event("system.delivery_failed", "Notification delivery failed", k.Category.SYSTEM, "notification_delivery", "A notification could not be delivered after the allowed attempts.",
          DefaultRule(({"type": k.Strategy.PERMISSION, "permission": "notification.deliveries"},), channels=(k.Channel.IN_APP,), priority=k.Priority.HIGH),
          "Notification delivery failed", "A notification could not be delivered ({{ channel }}: {{ error }}).\n\nReview deliveries: {{ link }}", "Notification delivery failed", "{{ channel }} delivery failed: {{ error }}.",
          (), ("channel", "error"), lambda s, m: {}, _delivery_context),
)}

CATEGORY_OF = {code: e.category for code, e in EVENTS.items()}


def get_event(code: str) -> Event | None:
    return EVENTS.get(str(code))


def describe_all() -> dict[str, Any]:
    return {
        "events": [{"code": e.code, "label": e.label, "category": e.category, "source_type": e.source_type, "description": e.description, "variables": list(e.variables),
                    "facts": [{"code": f.code, "label": f.label, "family": f.family, "kind": f.kind, "choices": list(f.choices) if f.choices else None} for f in e.facts]}
                   for e in EVENTS.values()],
        "sources": [{"type": s.type, "label": s.label} for s in SOURCES.values()],
        "strategies": [{"value": v, "label": str(label)} for v, label in k.Strategy.choices],
        "channels": [{"value": v, "label": str(label)} for v, label in k.Channel.choices],
        "priorities": [{"value": v, "label": str(label)} for v, label in k.Priority.choices],
        "policies": [{"value": v, "label": str(label)} for v, label in k.PreferencePolicy.choices],
        "categories": [{"value": v, "label": str(label)} for v, label in k.Category.choices],
    }


def absolute_link(path: str) -> str:
    """A safe link for an email: an *internal path* only, prefixed with the configured base URL (empty = the path alone)."""
    if not path.startswith("/") or path.startswith("//"):
        return ""
    return f"{str(getattr(settings, 'NOTIFICATION_BASE_URL', '') or '').rstrip('/')}{path}"

