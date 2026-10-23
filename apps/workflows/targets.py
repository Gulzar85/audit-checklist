"""The controlled **target registry**: the only way a workflow can be bound to a business record (ADR 0069).

A client never names a Django model, a content type or an arbitrary object: it names a registered ``target_type`` and an opaque id, and
trusted server code resolves the record through that module's *authorized selector* (so an invisible or foreign record is "not found"),
reports its organization and location, and exposes the **attributes** workflow conditions may use.  Adding a target type is deliberate:
write a ``TargetSpec``, register it, and register its business actions in ``handlers.py``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from apps.checklists.rules.operators import CHOICE, EQUALITY, ORDERED, TEXT  # noqa: F401  (comparison families: the Phase 6 vocabulary)
from apps.core.utils import parse_uuid


class TargetNotFound(Exception):
    """Unknown type, malformed / unknown / invisible id, or a record from another organization - all the same generic answer."""


@dataclass(frozen=True)
class Attribute:
    """A fact about the business record that a condition may compare (never arbitrary model data)."""

    code: str
    label: str
    family: str  # equality | choice | ordered | text  (the Phase 6 comparison family decides the operators)
    kind: str = "str"  # str | int | bool
    choices: tuple[str, ...] | None = None  # a fixed vocabulary, or None for organization-specific codes


@dataclass
class TargetSpec:
    type: str
    label: str
    attributes: dict[str, Attribute] = field(default_factory=dict)
    #: ``{event code: label}`` - the domain events that may auto-start a workflow of this type.
    events: dict[str, str] = field(default_factory=dict)

    # -- resolution ---------------------------------------------------------------------------
    def resolve(self, user: Any, pk: Any) -> Any:  # pragma: no cover - interface
        raise NotImplementedError

    def resolve_system(self, pk: Any) -> Any:  # pragma: no cover - interface
        """Load the record *without* a user (the engine's own re-reads and handlers; authorization is rechecked by the domain service)."""
        raise NotImplementedError

    def can_view(self, user: Any, obj: Any) -> bool:  # pragma: no cover - interface
        raise NotImplementedError

    # -- facts about a record -----------------------------------------------------------------
    def organization(self, obj: Any) -> Any:
        return obj.organization

    def location(self, obj: Any) -> Any:
        return obj.location

    def owner_id(self, obj: Any) -> Any:  # pragma: no cover - interface
        return None

    def creator_id(self, obj: Any) -> Any:  # pragma: no cover - interface
        return None

    def reference(self, obj: Any) -> str:
        return getattr(obj, "reference", str(obj.pk))

    def title(self, obj: Any) -> str:
        return getattr(obj, "title", self.reference(obj))

    def url(self, obj: Any) -> str:
        return obj.get_absolute_url()

    def facts(self, obj: Any) -> dict[str, Any]:  # pragma: no cover - interface
        raise NotImplementedError

    def eligible(self, obj: Any) -> str | None:
        """A message when the record cannot enter a workflow at all (e.g. it is already finished); ``None`` when it can."""
        return None


_REGISTRY: dict[str, TargetSpec] = {}


def register(spec: TargetSpec) -> None:
    _REGISTRY[spec.type] = spec


def get_spec(target_type: Any) -> TargetSpec:
    spec = _REGISTRY.get(str(target_type))
    if spec is None:
        raise TargetNotFound
    return spec


def registered_types() -> list[str]:
    return sorted(_REGISTRY)


def describe_all() -> list[dict[str, Any]]:
    return [{"type": s.type, "label": s.label, "events": s.events,
             "attributes": [{"code": a.code, "label": a.label, "family": a.family, "kind": a.kind, "choices": list(a.choices) if a.choices else None}
                            for a in s.attributes.values()]} for s in _REGISTRY.values()]


# ---------------------------------------------------------------------------------------------------------------------------------
class CorrectiveActionTarget(TargetSpec):
    def __init__(self) -> None:
        from apps.corrective_actions.constants import ActionStatus, Priority

        super().__init__(
            "corrective_action", "Corrective action",
            {a.code: a for a in (
                Attribute("priority", "Priority", CHOICE, "str", tuple(Priority.values)),
                Attribute("priority_rank", "Priority rank (1 low - 4 critical)", ORDERED, "int"),
                Attribute("status", "Status", CHOICE, "str", tuple(ActionStatus.values)),
                Attribute("required_evidence", "Required completion files", ORDERED, "int"),
                Attribute("severity", "Finding severity (code)", CHOICE, "str"),
                Attribute("severity_rank", "Finding severity rank", ORDERED, "int"),
                Attribute("category", "Finding category (code)", CHOICE, "str"),
                Attribute("has_owner", "Has a responsible user", EQUALITY, "bool"),
                Attribute("overdue", "Is overdue", EQUALITY, "bool"),
            )},
            {"corrective_action.submitted": "A corrective action is submitted for verification", "corrective_action.created": "A corrective action is created"},
        )

    def resolve(self, user: Any, pk: Any) -> Any:
        from apps.corrective_actions import selectors

        return selectors.get_action(user, pk)

    def resolve_system(self, pk: Any) -> Any:
        from apps.corrective_actions.models import CorrectiveAction

        parsed = parse_uuid(pk)
        return CorrectiveAction.objects.select_related("organization", "location", "finding__severity", "finding__category").filter(pk=parsed).first() if parsed else None

    def can_view(self, user: Any, obj: Any) -> bool:
        from apps.corrective_actions import policies

        return policies.can_view(user, obj)

    def owner_id(self, obj: Any) -> Any:
        return obj.assigned_to_id

    def creator_id(self, obj: Any) -> Any:
        return obj.created_by_id

    def facts(self, obj: Any) -> dict[str, Any]:
        f = obj.finding
        return {"priority": obj.priority, "priority_rank": obj.priority_rank, "status": obj.status, "required_evidence": obj.required_evidence,
                "severity": f.severity.code, "severity_rank": f.severity.rank, "category": f.category.code, "has_owner": obj.assigned_to_id is not None,
                "overdue": obj.is_overdue}

    def eligible(self, obj: Any) -> str | None:
        from apps.corrective_actions.constants import TERMINAL_STATUSES

        return "This corrective action is finished and cannot enter a workflow." if obj.status in TERMINAL_STATUSES else None


class FindingTarget(TargetSpec):
    def __init__(self) -> None:
        from apps.findings.constants import FindingStatus, SourceType

        super().__init__(
            "finding", "Finding",
            {a.code: a for a in (
                Attribute("severity", "Severity (code)", CHOICE, "str"),
                Attribute("severity_rank", "Severity rank", ORDERED, "int"),
                Attribute("category", "Category (code)", CHOICE, "str"),
                Attribute("status", "Status", CHOICE, "str", tuple(FindingStatus.values)),
                Attribute("source_type", "Source", CHOICE, "str", tuple(SourceType.values)),
                Attribute("needs_review", "Needs review", EQUALITY, "bool"),
                Attribute("escalation", "Severity is an escalation level", EQUALITY, "bool"),
                Attribute("has_owner", "Has a responsible user", EQUALITY, "bool"),
            )},
            {"finding.raised": "A finding is raised"},
        )

    def resolve(self, user: Any, pk: Any) -> Any:
        from apps.findings import selectors

        return selectors.get_finding(user, pk)

    def resolve_system(self, pk: Any) -> Any:
        from apps.findings.models import Finding

        parsed = parse_uuid(pk)
        return Finding.objects.select_related("organization", "location", "severity", "category").filter(pk=parsed).first() if parsed else None

    def can_view(self, user: Any, obj: Any) -> bool:
        from apps.findings import policies

        return policies.can_view(user, obj)

    def owner_id(self, obj: Any) -> Any:
        return obj.assigned_to_id

    def creator_id(self, obj: Any) -> Any:
        return obj.detected_by_id

    def facts(self, obj: Any) -> dict[str, Any]:
        return {"severity": obj.severity.code, "severity_rank": obj.severity.rank, "category": obj.category.code, "status": obj.status,
                "source_type": obj.source_type, "needs_review": obj.needs_review, "escalation": obj.severity.is_escalation, "has_owner": obj.assigned_to_id is not None}

    def eligible(self, obj: Any) -> str | None:
        from apps.findings.constants import TERMINAL_STATUSES

        return "This finding is finished and cannot enter a workflow." if obj.status in TERMINAL_STATUSES else None


class ExecutionTarget(TargetSpec):
    """A submitted checklist execution (Phase 16): a workflow can be requested for it by an evaluation rule or started by an authorized user.

    There are no business actions for this target (the checklist execution has no lifecycle decision a workflow could take over), so its
    workflows are pure approval / review processes whose tasks are completed by people.
    """

    def __init__(self) -> None:
        from apps.executions.constants import ExecutionStatus

        super().__init__(
            "execution", "Checklist execution",
            {a.code: a for a in (Attribute("status", "Execution status", CHOICE, "str", tuple(ExecutionStatus.values)),)},
            {},
        )

    def resolve(self, user: Any, pk: Any) -> Any:
        from apps.executions import selectors

        return selectors.get_execution(user, pk)

    def resolve_system(self, pk: Any) -> Any:
        from apps.executions.models import ChecklistExecution

        parsed = parse_uuid(pk)
        return ChecklistExecution.objects.select_related("organization", "assignment__location").filter(pk=parsed).first() if parsed else None

    def can_view(self, user: Any, obj: Any) -> bool:
        from apps.executions import policies

        return policies.can_view(user, obj)

    def location(self, obj: Any) -> Any:
        return obj.assignment.location

    def owner_id(self, obj: Any) -> Any:
        return obj.assignment.assigned_to_id

    def creator_id(self, obj: Any) -> Any:
        return obj.submitted_by_id or obj.started_by_id

    def title(self, obj: Any) -> str:
        return f"{obj.reference} - {obj.version.template.name}"

    def facts(self, obj: Any) -> dict[str, Any]:
        return {"status": obj.status}

    def eligible(self, obj: Any) -> str | None:
        from apps.executions.constants import ExecutionStatus

        return None if obj.status == ExecutionStatus.SUBMITTED else "Only a submitted checklist execution can enter a workflow."


def register_defaults() -> None:
    register(CorrectiveActionTarget())
    register(FindingTarget())
    register(ExecutionTarget())
