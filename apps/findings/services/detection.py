"""``FindingDetectionService``: turn the Phase 6 ``RAISE_FINDING`` rule outcomes of a *submitted* execution into findings.

Timing (ADR 0064).  Findings are raised **when an execution is submitted**, inside the submission transaction, never while it is in
progress: a draft answer can still change, so a finding raised from it would be provisional and would have to be retracted.  The
in-progress workspace therefore makes no promise; ``preview`` (read-only) shows what the current answers would raise.

Reuse, not a second engine.  The conditions are the Phase 6 operators evaluated by ``apps.checklists.rules`` over the answers
Phase 9 validated (``build_state``); this module only reads ``Evaluation.items[code].findings`` and persists them.  Hidden items,
unanswered / N/A / invalid answers raise nothing unless a rule's condition explicitly matches them (``IS_EMPTY`` ...): the rule
engine's three-valued logic already makes an unknown condition not act.

Idempotency.  A finding's identity is ``detection_key = <execution>:<item>:<rule>#<action index>``, enforced by a partial unique
constraint, so repeated or concurrent runs (a retry, a re-run command, an offline resubmission) return the existing finding.  A
finding is never withdrawn when evidence later changes: submitted responses are frozen, and a cancelled finding is not recreated.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.checklists.rules.actions import FINDING_TOKENS

from .. import events
from ..constants import SourceType
from ..models import Finding, FindingCategory, FindingSeverity
from . import config
from .base import VERBS, lock_organization, log, next_number

_TITLE_DEFAULT = "{question}"


@dataclass
class DetectionOutcome:
    created: list[Finding] = field(default_factory=list)
    existing: list[Finding] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)

    @property
    def status(self) -> str:
        return "created" if self.created else "exists" if self.existing else "none"

    def as_dict(self) -> dict[str, Any]:
        return {"status": self.status, "created": [str(f.pk) for f in self.created], "existing": [str(f.pk) for f in self.existing], "problems": self.problems}


def _answer_text(result: Any) -> str:
    value = result.answer.serialized
    if value is None:
        return "no answer" if result.answer.status != "not_applicable" else "not applicable"
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if isinstance(value, (list, tuple)):
        return ", ".join(str(v) for v in value)
    if isinstance(value, dict):
        return "provided"
    return str(value)


def render(template: str, values: dict[str, str], maximum: int) -> str:
    """Fill the approved placeholders by plain substitution (``str.format`` would expose attributes); unknown braces are left as text."""
    for token in FINDING_TOKENS:
        template = template.replace("{" + token + "}", values.get(token, ""))
    return " ".join(template.split())[:maximum]


def _resolve(organization: Any, spec: dict[str, Any]) -> tuple[FindingCategory | None, FindingSeverity | None, list[str]]:
    """The organization's category / severity for the rule's codes; a missing one falls back to the default and flags the finding for review."""
    notes: list[str] = []
    category = FindingCategory.objects.filter(organization=organization, code=spec["category"], is_active=True).first()
    severity = FindingSeverity.objects.filter(organization=organization, code=spec["severity"], is_active=True).first()
    if category is None:
        category = (FindingCategory.objects.filter(organization=organization, is_active=True, code="other").first()
                    or FindingCategory.objects.filter(organization=organization, is_active=True).order_by("sort_order").first())
        notes.append(f"unknown category “{spec['category']}”")
    if severity is None:
        severity = (FindingSeverity.objects.filter(organization=organization, is_active=True, is_default=True).first()
                    or FindingSeverity.objects.filter(organization=organization, is_active=True).order_by("rank").first())
        notes.append(f"unknown severity “{spec['severity']}”")
    return category, severity, notes


def collect(state: Any) -> list[tuple[str, Any, dict[str, Any]]]:
    """``[(item code, ItemResult, finding spec)]`` for every matched, visible ``RAISE_FINDING`` of the evaluated execution, in a stable order."""
    out = []
    for code, result in sorted(state.evaluation.items.items()):
        for spec in result.findings:
            out.append((code, result, spec))
    return out


def preview(execution: Any) -> list[dict[str, Any]]:
    """What the *current* answers would raise - read-only, non-authoritative (nothing is stored, nothing is promised)."""
    from apps.executions.services.evaluation import build_state

    state = build_state(execution)
    return [{"item": state.snapshot.items[code].question, "rule": spec["rule"], "category": spec["category"], "severity": spec["severity"]}
            for code, _result, spec in collect(state)]


def detect_for_execution(execution: Any, *, actor: Any = None) -> DetectionOutcome:
    """Raise the findings a *submitted* execution's rules call for.  Idempotent; safe to call again at any time (retry, command)."""
    from apps.executions.constants import ExecutionStatus
    from apps.executions.models import ChecklistResponse
    from apps.executions.services.evaluation import build_state

    from .errors import FindingError

    if execution.status != ExecutionStatus.SUBMITTED:
        raise FindingError("Findings are raised only for submitted executions.", "not_submitted")
    outcome = DetectionOutcome()
    state = build_state(execution)
    wanted = collect(state)
    if not wanted:
        return outcome
    organization = execution.organization
    assignment, location = execution.assignment, execution.assignment.location
    with transaction.atomic():
        lock_organization(organization.pk)  # serialises numbering with manual creation and concurrent detection
        config.ensure_defaults(organization)
        detected_at = execution.submitted_at or timezone.now()
        for code, result, spec in wanted:
            item = state.snapshot.items[code]
            key = f"{execution.pk}:{item.id}:{spec['rule']}#{spec['index']}"
            found = Finding.objects.filter(organization=organization, detection_key=key).first()
            if found is not None:
                outcome.existing.append(found)
                continue
            category, severity, notes = _resolve(organization, spec)
            if category is None or severity is None:
                outcome.problems.append(f"{spec['rule']}: the organization has no active category or severity level")
                continue
            values = {"question": item.question, "answer": _answer_text(result), "location": location.name, "checklist": state.execution.version.template.name}
            title = render(spec.get("title") or _TITLE_DEFAULT, values, 200) or item.question[:200]
            description = render(spec.get("description", ""), values, 2000)
            due = detected_at + timedelta(days=severity.default_due_days) if severity.default_due_days else None
            response = ChecklistResponse.objects.filter(execution=execution, item_id=item.id).first()
            finding = Finding(
                organization=organization, number=next_number(organization.pk), title=title, description=description, category=category,
                severity=severity, source_type=SourceType.CHECKLIST_RULE, needs_review=bool(spec.get("requires_review")) or bool(notes),
                inherit_evidence=bool(spec.get("inherit_evidence", True)), location=location, assignment=assignment, execution=execution,
                version=execution.version, item_id=item.id, response=response, rule_code=spec["rule"], rule_name=spec.get("rule_name", "")[:200],
                detection_key=key, detected_at=detected_at, detected_by=execution.submitted_by or actor, due_at=due,
            )
            try:
                with transaction.atomic():  # a savepoint: a lost race on the unique key must not poison the outer transaction
                    finding.save()
            except IntegrityError:
                racer = Finding.objects.filter(organization=organization, detection_key=key).first()
                if racer is None:
                    raise
                outcome.existing.append(racer)
                continue
            outcome.created.append(finding)
            log(VERBS["raised"], finding, actor or execution.submitted_by, f"Rule “{finding.rule_name or finding.rule_code}” raised {finding.reference}: {finding.title}",
                source=SourceType.CHECKLIST_RULE, rule=spec["rule"], execution_id=str(execution.pk), item_id=str(item.id), severity=severity.code,
                category=category.code, needs_review=finding.needs_review, config_notes=notes or None)
            events.send_on_commit(events.finding_raised, finding, actor or execution.submitted_by, source=SourceType.CHECKLIST_RULE)
    from apps.core.services import log_activity

    log_activity(verb=VERBS["detection_run"], organization=organization, actor=actor or execution.submitted_by, target=execution,
                 description=f"Finding detection for {execution.reference}: {len(outcome.created)} raised, {len(outcome.existing)} already existed",
                 metadata={"execution_id": str(execution.pk), "created": len(outcome.created), "existing": len(outcome.existing), "problems": outcome.problems[:20]})
    return outcome


def detect_after_submission(execution: Any, actor: Any) -> DetectionOutcome | None:
    """Called by the submission service inside its transaction.  A *bug* here must never lose the submission: it is recorded and
    the findings can be raised later (``manage.py detect_findings``, idempotent)."""
    import logging

    try:
        with transaction.atomic():
            return detect_for_execution(execution, actor=actor)
    except Exception:  # noqa: BLE001 - deliberately broad: see the docstring
        logging.getLogger(__name__).exception("Finding detection failed for execution %s", execution.pk)
        from apps.core.services import log_activity

        log_activity(verb=VERBS["detection_failed"], organization=execution.organization, actor=actor, target=execution,
                     description=f"Finding detection for {execution.reference} failed; run detect_findings to retry",
                     metadata={"execution_id": str(execution.pk)})
        return None


__all__ = ["DetectionOutcome", "collect", "detect_after_submission", "detect_for_execution", "preview", "render"]
