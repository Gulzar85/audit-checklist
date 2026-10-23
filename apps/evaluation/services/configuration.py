"""Writing and validating the evaluation rules of a *draft* checklist version, and the hooks the version lifecycle calls.

Same discipline as scoring: every write locks the template through the checklists service, refuses anything but a draft, logs through the shared
activity log.  A draft may hold incomplete rules (they are kept as typed and flagged by validation); a rule that compiles is stored in its normalized
form, so equivalent input hashes identically.  Publication refuses any rule with an error.
"""

from __future__ import annotations

import copy
from typing import Any

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction

from apps.checklists import services as checklist_services
from apps.checklists.rules import snapshot_for_version
from apps.core.services import log_activity, stamp_history

from .. import config as cfg
from .. import constants as k
from .. import policies
from ..models import EvaluationRuleSet
from .errors import EvaluationError

VERBS = {"saved": "evaluation.ruleset_saved", "removed": "evaluation.ruleset_removed", "cloned": "evaluation.ruleset_cloned"}


class OrgLookups(cfg.Lookups):
    """The organization's finding categories / severities and the workflows available for executions (checked at validation, never trusted at run time)."""

    def __init__(self, organization: Any) -> None:
        self.organization = organization
        self._cache: dict[tuple[str, str], bool] = {}

    def _memo(self, kind: str, code: str, compute: Any) -> bool:
        if (kind, code) not in self._cache:
            self._cache[(kind, code)] = bool(compute())
        return self._cache[(kind, code)]

    def category(self, code: str) -> bool:
        from apps.findings.models import FindingCategory

        return self._memo("c", code, lambda: FindingCategory.objects.filter(organization=self.organization, code=code, is_active=True).exists())

    def severity(self, code: str) -> bool:
        from apps.findings.models import FindingSeverity

        return self._memo("s", code, lambda: FindingSeverity.objects.filter(organization=self.organization, code=code, is_active=True).exists())

    def workflow(self, code: str) -> bool:
        from django.db.models import Q

        from apps.workflows.constants import DefinitionStatus, VersionStatus
        from apps.workflows.models import WorkflowDefinition

        return self._memo("w", code, lambda: WorkflowDefinition.objects.filter(
            Q(organization=self.organization) | Q(organization__isnull=True), code=code, target_type="execution", status=DefinitionStatus.ACTIVE,
            versions__status=VersionStatus.PUBLISHED).exists())


def _log(verb: str, version: Any, actor: Any, description: str, **meta: Any) -> None:
    log_activity(verb=verb, organization=version.template.organization, actor=actor, target=version, description=description,
                 metadata={"template_id": str(version.template_id), "version": version.version_number, **meta})


def _editable(version: Any) -> tuple[Any, Any]:
    try:
        return checklist_services._editable(version)
    except ValidationError as exc:
        raise EvaluationError(" ".join(exc.messages), getattr(exc, "code", None) or "version_immutable") from exc


def _sort_key(rule: Any) -> tuple[int, str]:
    priority = rule.get("priority") if isinstance(rule, dict) else None
    return (priority if isinstance(priority, int) and not isinstance(priority, bool) else 10**9, str(rule.get("code", "")) if isinstance(rule, dict) else "")


def normalize_for_save(snapshot: Any, document: Any, lookups: cfg.Lookups | None = None) -> dict[str, Any]:
    """Structurally sound document for a draft: compilable rules in normalized form, incomplete ones kept as typed; refuses a document that is not a rules list."""
    if not isinstance(document, dict) or set(document) - {"rules"} or not isinstance(document.get("rules", []), list):
        raise EvaluationError('The rule set must be {"rules": [ ... ]}.', "malformed")
    raw_rules = document.get("rules", [])
    if len(raw_rules) > k.MAX_RULES:
        raise EvaluationError(f"A rule set may have at most {k.MAX_RULES} rules.", "too_many_rules")
    out = []
    for position, raw in enumerate(raw_rules, 1):
        if not isinstance(raw, dict):
            raise EvaluationError("Every rule must be an object.", "malformed")
        issues: list[cfg.Issue] = []
        compiled = cfg.compile_rule(snapshot, raw, position, lookups, issues)
        out.append(compiled.stored if compiled is not None else copy.deepcopy(raw))
    try:
        import json

        json.dumps(out)
    except (TypeError, ValueError) as exc:
        raise EvaluationError("The rule set contains values that cannot be stored.", "malformed") from exc
    return {"rules": sorted(out, key=_sort_key)}


@transaction.atomic
def save_config(version: Any, *, actor: Any, enabled: bool | None = None, document: Any = None, notes: str | None = None) -> EvaluationRuleSet:
    """Create or update the draft's rule set.  ``document`` may be a callable ``f(current) -> new`` to edit part of it atomically (see ``set_rule``)."""
    if not policies.can_configure_at(actor, version):
        raise PermissionDenied
    template, fresh = _editable(version)
    row = EvaluationRuleSet.objects.select_for_update().filter(version=fresh).first()
    if enabled is not None and not isinstance(enabled, bool):
        raise EvaluationError("“enabled” must be true or false.", "invalid_setting")
    current = copy.deepcopy(row.document) if row else cfg.empty_document()
    if callable(document):
        document = document(current)
    snapshot = snapshot_for_version(fresh, include_rules=False)
    normalized = normalize_for_save(snapshot, document if document is not None else current)
    created = row is None
    if created:
        row = EvaluationRuleSet(version=fresh, organization_id=template.organization_id, created_by=actor)
    if enabled is not None:
        row.enabled = enabled
    row.document = normalized
    if notes is not None:
        row.notes = notes.strip()[:500]
    row.updated_by = actor
    stamp_history(row, actor, VERBS["saved"])
    row.save()
    checklist_services._touch(template, actor)
    _log(VERBS["saved"], fresh, actor, f"Evaluation rules {'created' if created else 'updated'} on {template.code} v{fresh.version_number}",
         rules=len(normalized["rules"]), enabled=row.enabled)
    return row


def set_rule(version: Any, *, actor: Any, code: str, rule: dict[str, Any] | None, original_code: str | None = None) -> EvaluationRuleSet:
    """Add or replace the rule ``original_code or code`` with ``rule`` (``None`` removes it)."""
    target = original_code or code

    def edit(doc: dict[str, Any]) -> dict[str, Any]:
        rules = [r for r in doc.get("rules", []) if not (isinstance(r, dict) and r.get("code") == target)]
        if rule is not None:
            if any(isinstance(r, dict) and r.get("code") == rule.get("code") for r in rules):
                raise EvaluationError(f"The rule code {rule.get('code')} is already used.", "duplicate_code")
            rules.append(rule)
        return {"rules": rules}

    return save_config(version, actor=actor, document=edit)


def toggle_rule(version: Any, *, actor: Any, code: str, enabled: bool) -> EvaluationRuleSet:
    def edit(doc: dict[str, Any]) -> dict[str, Any]:
        rules = [({**r, "enabled": enabled} if isinstance(r, dict) and r.get("code") == code else r) for r in doc.get("rules", [])]
        return {"rules": rules}

    return save_config(version, actor=actor, document=edit)


def move_rule(version: Any, *, actor: Any, code: str, direction: str) -> EvaluationRuleSet:
    """Swap the priority of a rule with its neighbour (``up`` = runs earlier).  Priorities stay unique."""
    def edit(doc: dict[str, Any]) -> dict[str, Any]:
        rules = sorted([r for r in doc.get("rules", []) if isinstance(r, dict)], key=_sort_key)
        index = next((i for i, r in enumerate(rules) if r.get("code") == code), None)
        if index is None:
            raise EvaluationError(f"There is no rule {code}.", "unknown_rule")
        other = index - 1 if direction == "up" else index + 1
        if 0 <= other < len(rules):
            a, b = rules[index].get("priority"), rules[other].get("priority")
            if isinstance(a, int) and isinstance(b, int) and a != b:
                rules[index] = {**rules[index], "priority": b}
                rules[other] = {**rules[other], "priority": a}
        return {"rules": rules}

    return save_config(version, actor=actor, document=edit)


@transaction.atomic
def remove_config(version: Any, *, actor: Any) -> None:
    if not policies.can_configure_at(actor, version):
        raise PermissionDenied
    template, fresh = _editable(version)
    row = EvaluationRuleSet.objects.select_for_update().filter(version=fresh).first()
    if row is None:
        return
    row.delete()
    checklist_services._touch(template, actor)
    _log(VERBS["removed"], fresh, actor, f"Evaluation rules removed from {template.code} v{fresh.version_number}")


def validate_config(version: Any, *, enabled: bool | None = None, document: Any = None) -> tuple[list[cfg.Issue], list[cfg.CompiledRule], dict[str, Any]]:
    """Issues of the saved (or a candidate) rule set against the version's real questions and the organization's reference data."""
    row = EvaluationRuleSet.objects.filter(version=version).first()
    if row is None and enabled is None and document is None:
        return [], [], cfg.empty_document()
    snapshot = snapshot_for_version(version, include_rules=False)
    effective = enabled if enabled is not None else (row.enabled if row else True)
    return cfg.validate_document(effective, document if document is not None else (row.document if row else cfg.empty_document()), snapshot,
                                 OrgLookups(version.template.organization))


# ---------------------------------------------------------------------------------------------------------------------------------
# Hooks into the version lifecycle (registered by ``integration``)
# ---------------------------------------------------------------------------------------------------------------------------------
def manifest_extension(version: Any) -> tuple[str, Any] | None:
    row = EvaluationRuleSet.objects.filter(version=version).first()
    return None if row is None else ("evaluation", cfg.canonical(row.enabled, row.document))


def publication_findings(version: Any) -> list[tuple[str, str, str, str]]:
    row = EvaluationRuleSet.objects.filter(version=version).first()
    if row is None or not row.enabled:
        return []
    issues, _compiled, _doc = cfg.validate_document(row.enabled, row.document, snapshot_for_version(version, include_rules=False), OrgLookups(version.template.organization))
    return [(i.level, f"evaluation_{i.code}", f"Evaluation rules: {i.message}", i.subject) for i in issues]


def clone_config(source: Any, target: Any, actor: Any) -> dict[str, int]:
    row = EvaluationRuleSet.objects.filter(version=source).first()
    if row is None:
        return {"evaluation": 0}
    copy_row = EvaluationRuleSet(version=target, organization_id=row.organization_id, enabled=row.enabled, document=copy.deepcopy(row.document), notes=row.notes,
                                 created_by=actor if getattr(actor, "is_authenticated", False) else None)
    stamp_history(copy_row, actor, VERBS["cloned"])
    copy_row.save()
    return {"evaluation": 1}
