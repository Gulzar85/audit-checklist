"""Bulk creation of checklist items from tabular rows (CSV / spreadsheet) - the authorization boundary.

This is deliberately a *service-level* import, not a model import: every row goes through the same
policies and services as the builder, so an import cannot do anything a user could not do by hand.

* The target is a template **code in one organization the caller may edit**; an unknown template and
  a template the caller may not see give the same "not found" message (no tenant probing).
* Only the template's **draft** version can be imported into; a published version is immutable.
* The whole file is validated and applied in one transaction: any bad row rolls everything back, so a
  failed import never leaves a half-built checklist.
* Sections are matched by name (created when missing); item codes, answer types, config and option
  lists are validated by the same code the UI uses.

Columns: ``template``, ``section``, ``question`` (required); ``code``, ``answer_type`` (default
``yes_no``), ``is_required`` (default true), ``config`` (JSON), ``options`` (``value:label|value:label``
or just labels), ``help_text``, ``description``, ``instructions``, and the descriptive metadata columns ``points``,
``critical`` and ``applies_to`` (comma-separated tags) - all optional.  Answer type, configuration, options and
metadata are validated by the same answer-type engine as the builder, with row-level error messages.

Rules (Phase 6) are imported the same way by :func:`import_rules`: columns ``template``, ``name`` and ``actions``
(JSON list) are required; ``code``, ``description``, ``enabled`` (default true) and ``condition`` (JSON) are optional.
Every row goes through ``services.create_rule`` - references, operators, values, cycles and conflicts are validated for
the *whole* rule set after each row, so an import can never produce a rule set the builder would refuse - and the file is
all-or-nothing with a row-numbered error for every problem.  :func:`export_rules` writes the same columns back out.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from typing import Any

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction

from apps.core.policies import is_active_user

from . import constants as c
from . import policies, selectors, services
from .models import ChecklistRule, ChecklistTemplate
from .services import RuleValidationError

_TRUE = {"1", "true", "yes", "y", "on", "required"}
_NOT_FOUND = "Template “{code}” was not found, or you are not allowed to import into it."
REQUIRED_COLUMNS = ("template", "section", "question")


def _bool(value: Any, default: bool = True) -> bool:
    if value in (None, ""):
        return default
    return str(value).strip().lower() in _TRUE


def _options(value: Any) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for part in str(value or "").split("|"):
        part = part.strip()
        if not part:
            continue
        key, sep, label = part.partition(":")
        out.append((key.strip(), label.strip()) if sep else ("", part))
    return out


def _metadata(row: dict[str, Any]) -> dict[str, Any]:
    meta: dict[str, Any] = {}
    if str(row.get("points") or "").strip():
        try:
            meta["points"] = int(str(row["points"]).strip())
        except ValueError as exc:
            raise ValidationError("Points must be a whole number.") from exc
    if str(row.get("critical") or "").strip():
        meta["critical"] = _bool(row["critical"], default=False)
    if str(row.get("applies_to") or "").strip():
        meta["applies_to"] = [t.strip() for t in str(row["applies_to"]).split(",") if t.strip()]
    return meta


def _config(value: Any) -> Any:
    if value in (None, ""):
        return None
    if isinstance(value, dict):
        return value
    try:
        return json.loads(str(value))
    except ValueError as exc:
        raise ValidationError("Config must be valid JSON.") from exc


@transaction.atomic
def import_items(*, user: Any, organization: Any, rows: Iterable[dict[str, Any]]) -> dict[str, int]:
    """Create items from ``rows`` in ``organization``'s draft versions; all or nothing."""
    if not is_active_user(user):
        raise PermissionDenied("Imports require an authenticated, active user.")
    rows = list(rows)
    errors: list[str] = []
    created = 0
    drafts: dict[str, Any] = {}
    sections: dict[tuple[str, str], Any] = {}
    allowed = {str(pk) for pk in selectors.organization_ids_with(user, "checklist.update")}
    if str(getattr(organization, "pk", organization)) not in allowed:
        raise PermissionDenied("You may not import checklists into this organization.")
    for number, row in enumerate(rows, start=2):  # row 1 is the header
        try:
            missing = [col for col in REQUIRED_COLUMNS if not str(row.get(col) or "").strip()]
            if missing:
                raise ValidationError(f"Missing: {', '.join(missing)}.")
            code = str(row["template"]).strip().upper()
            if code not in drafts:
                template = ChecklistTemplate.objects.filter(organization=organization, code=code).first()
                draft = selectors.get_draft_version(template) if template else None
                if template is None or draft is None or not policies.can_edit_version(user, draft):
                    raise ValidationError(_NOT_FOUND.format(code=row["template"]))
                drafts[code] = draft
            draft = drafts[code]
            name = str(row["section"]).strip()
            if (code, name) not in sections:
                sections[(code, name)] = draft.sections.filter(name=name).first() or services.create_section(
                    draft, name=name, actor=user
                )
            services.create_item(
                sections[(code, name)], question=str(row["question"]), actor=user,
                code=str(row.get("code") or ""), answer_type=str(row.get("answer_type") or c.AnswerType.YES_NO).strip(),
                config=_config(row.get("config")), is_required=_bool(row.get("is_required")),
                help_text=str(row.get("help_text") or ""), description=str(row.get("description") or ""),
                instructions=str(row.get("instructions") or ""), options=_options(row.get("options")),
                metadata=_metadata(row),
            )
            created += 1
        except ValidationError as exc:
            errors.append(f"Row {number}: {' '.join(exc.messages)}")
    if errors:
        transaction.set_rollback(True)
        raise ValidationError(errors)
    return {"items": created, "sections": len(sections)}


# ---------------------------------------------------------------------------
# Rules
# ---------------------------------------------------------------------------
RULE_REQUIRED_COLUMNS = ("template", "name", "actions")
RULE_COLUMNS = ("template", "code", "name", "description", "enabled", "condition", "actions")
MAX_RULE_CELL = 20_000


def _json_cell(value: Any, what: str) -> Any:
    """A JSON cell: already-parsed data (from a JSON/API source) or a bounded JSON string; never evaluated."""
    if value in (None, ""):
        return None
    if isinstance(value, (dict, list)):
        return value
    text = str(value)
    if len(text) > MAX_RULE_CELL:
        raise ValidationError(f"{what} may be at most {MAX_RULE_CELL} characters.")
    try:
        return json.loads(text)
    except (ValueError, RecursionError) as exc:
        raise ValidationError(f"{what} must be valid JSON.") from exc


def _rule_messages(exc: ValidationError) -> str:
    if isinstance(exc, RuleValidationError):
        return " ".join(f"{i.path + ': ' if i.path else ''}{i.message}" for i in exc.issues)
    return " ".join(exc.messages)


@transaction.atomic
def import_rules(*, user: Any, organization: Any, rows: Iterable[dict[str, Any]]) -> dict[str, int]:
    """Create rules from ``rows`` in ``organization``'s draft versions; all or nothing, with a message per bad row."""
    if not is_active_user(user):
        raise PermissionDenied("Imports require an authenticated, active user.")
    rows = list(rows)
    allowed = {str(pk) for pk in selectors.organization_ids_with(user, "checklist.update")}
    if str(getattr(organization, "pk", organization)) not in allowed:
        raise PermissionDenied("You may not import checklists into this organization.")
    errors: list[str] = []
    created = 0
    drafts: dict[str, Any] = {}
    for number, row in enumerate(rows, start=2):  # row 1 is the header
        try:
            missing = [col for col in RULE_REQUIRED_COLUMNS if not str(row.get(col) or "").strip() and not isinstance(row.get(col), (list, dict))]
            if missing:
                raise ValidationError(f"Missing: {', '.join(missing)}.")
            code = str(row["template"]).strip().upper()
            if code not in drafts:
                template = ChecklistTemplate.objects.filter(organization=organization, code=code).first()
                draft = selectors.get_draft_version(template) if template else None
                if template is None or draft is None or not policies.can_edit_version(user, draft):
                    raise ValidationError(_NOT_FOUND.format(code=row["template"]))
                drafts[code] = draft
            services.create_rule(
                drafts[code], name=str(row["name"]), description=str(row.get("description") or ""), code=str(row.get("code") or ""),
                condition=_json_cell(row.get("condition"), "Condition"), actions=_json_cell(row.get("actions"), "Actions"),
                is_active=_bool(row.get("enabled")), actor=user,
            )
            created += 1
        except ValidationError as exc:
            errors.append(f"Row {number}: {_rule_messages(exc)}")
    if errors:
        transaction.set_rollback(True)
        raise ValidationError(errors)
    return {"rules": created, "templates": len(drafts)}


def export_rules(*, user: Any, template: Any) -> list[dict[str, Any]]:
    """The rules of a template's draft (else latest published) version as importable rows.

    ``template`` is resolved through the authorized selector, so a template the caller cannot see exports nothing (404-style).
    """
    visible = selectors.get_template(user, getattr(template, "pk", template))
    if visible is None:
        raise ValidationError("Template not found.")
    version = selectors.get_draft_version(visible) or visible.versions.filter(status=c.VersionStatus.PUBLISHED).order_by("-version_number").first()
    if version is None:
        return []
    return [
        {"template": visible.code, "code": r.code, "name": r.name, "description": r.description, "enabled": r.is_active,
         "condition": json.dumps(r.condition, ensure_ascii=False) if r.condition else "", "actions": json.dumps(r.actions, ensure_ascii=False)}
        for r in ChecklistRule.objects.filter(version=version).order_by("sort_order")
    ]
