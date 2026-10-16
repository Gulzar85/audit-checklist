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
or just labels), ``help_text``, ``description``, ``instructions`` (optional).
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
from .models import ChecklistTemplate

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
            )
            created += 1
        except ValidationError as exc:
            errors.append(f"Row {number}: {' '.join(exc.messages)}")
    if errors:
        transaction.set_rollback(True)
        raise ValidationError(errors)
    return {"items": created, "sections": len(sections)}
