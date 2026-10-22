"""The offline *package*: everything a device needs to run one assignment without a connection (read-only, authorized).

It is a rendering of the exact published version the assignment is pinned to - the same definition the server evaluates - plus the
assignment's metadata and, if an execution already exists, its saved answers with their revisions (the device's sync base).
The package is content-hashed (``package_hash``) so the client can verify what it stored and the server can recognise a changed
definition; it contains no credentials, no permissions and no other users' data.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from django.utils import timezone

from apps.assignments import selectors as assignment_selectors
from apps.assignments.models import ChecklistAssignment
from apps.checklists.answer_types import registry
from apps.checklists.rules import compile_rules, snapshot_for_version
from apps.checklists.selectors import get_sections_for_version

from .. import selectors
from ..models import ChecklistExecution, ChecklistResponse
from . import compat


def canonical(data: Any) -> bytes:
    """Deterministic JSON bytes (sorted keys, compact) - the basis of every hash and fingerprint."""
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str).encode("utf-8")


def sha256(data: Any) -> str:
    return hashlib.sha256(canonical(data)).hexdigest()


class VersionCompat:
    """Compatibility of a version, computed once per version per request."""

    def __init__(self) -> None:
        self._cache: dict[Any, list[str]] = {}

    def reasons(self, version: Any) -> list[str]:
        if version.pk not in self._cache:
            snapshot = snapshot_for_version(version)
            compiled = compile_rules(snapshot)
            reasons = compat.compatibility(snapshot, compiled)
            if any(i.level == "error" for i in compiled.issues):
                reasons.append("the checklist's rules contain errors")
            self._cache[version.pk] = list(dict.fromkeys(reasons))
        return self._cache[version.pk]


def execution_state(execution: ChecklistExecution | None) -> dict[str, Any] | None:
    if execution is None:
        return None
    rows = ChecklistResponse.objects.filter(execution=execution, is_hidden=False).select_related("item")
    return {
        "id": str(execution.pk), "reference": execution.reference, "status": execution.status, "revision": execution.revision,
        "responses": [{"item": str(r.item_id), "code": r.item.code, "status": r.status, "value": r.value, "comment": r.comment, "revision": r.revision}
                      for r in rows],
    }


def build_package(user: Any, assignment: ChecklistAssignment) -> dict[str, Any]:
    """The package for ``assignment`` (the caller already authorized it).  Raises ``Unsupported`` if it cannot run offline."""
    version = assignment.version
    reasons = VersionCompat().reasons(version)
    if reasons:
        raise Unsupported(reasons)
    snapshot = snapshot_for_version(version)
    compiled = compile_rules(snapshot)
    sections, items = [], []
    for section in get_sections_for_version(version):
        sections.append({"id": str(section.pk), "code": section.code, "name": section.name, "description": section.description,
                         "instructions": section.instructions, "sort_order": section.sort_order})
        for item in section.items.all():
            if not item.is_active:
                continue
            definition = registry.get(item.answer_type)
            items.append({
                "id": str(item.pk), "code": item.code, "section": section.code, "question": item.question, "help_text": item.help_text,
                "instructions": item.instructions, "answer_type": item.answer_type, "config": definition.config_with_defaults(item.config),
                "required": item.is_required, "sort_order": item.sort_order,
                "options": [{"value": o.value, "label": o.label} for o in item.options.all() if o.is_active],
            })
    rules = [{"code": r.code, "name": r.name, "condition": r.stored_condition, "actions": r.stored_actions} for r in compiled.active]
    paths = assignment_selectors.location_paths({assignment.organization_id})
    execution = selectors.execution_for_assignment(user, assignment)
    body = {
        "schema_version": compat.SCHEMA_VERSION, "evaluator_version": compat.EVALUATOR_VERSION,
        "assignment": {
            "id": str(assignment.pk), "reference": assignment.reference, "status": assignment.status, "priority": assignment.priority,
            "due_at": assignment.due_at, "instructions": assignment.instructions, "organization": str(assignment.organization_id),
            "location": {"id": str(assignment.location_id), "name": assignment.location.name,
                         "path": paths.get(assignment.location_id, assignment.location.name)},
        },
        "checklist": {"template": str(version.template_id), "name": version.template.name, "code": version.template.code,
                      "version_id": str(version.pk), "version_number": version.version_number, "integrity_hash": version.integrity_hash},
        "sections": sections, "items": items, "rules": rules,
    }
    return {**body, "package_hash": sha256(body), "execution": execution_state(execution), "generated_at": timezone.now()}


class Unsupported(Exception):
    def __init__(self, reasons: list[str]) -> None:
        super().__init__("; ".join(reasons))
        self.reasons = reasons


def offline_assignments(user: Any):  # noqa: ANN201
    """The open assignments the user may prepare (the same set as the worklist), cheapest query first."""
    return selectors.my_assignments(user).order_by("due_at", "-created_at")
