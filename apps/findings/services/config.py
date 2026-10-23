"""Organization configuration of the findings engine: categories and severity levels.

Defaults are seeded lazily (``ensure_defaults``) and idempotently.  Every change requires ``finding.manage`` (organization-wide),
is validated here once (web, API and admin all call these functions) and audited.  Nothing is hard-deleted: a category or severity
can only be deactivated, so a finding always keeps pointing at what it was classified with.
"""

from __future__ import annotations

import re
from typing import Any

from django.core.exceptions import PermissionDenied
from django.db import IntegrityError, transaction
from django.utils.translation import gettext_lazy as _

from apps.core.services import log_activity, stamp_history

from .. import policies
from ..constants import DEFAULT_CATEGORIES, DEFAULT_SEVERITIES
from ..models import FindingCategory, FindingSeverity
from .base import VERBS
from .errors import FindingConflict, FindingError

_CODE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")


@transaction.atomic
def ensure_defaults(organization: Any) -> None:
    """Copy the starting categories and severity levels into an organization that has none (never touches existing rows)."""
    if not FindingCategory.objects.filter(organization=organization).exists():
        for order, (code, name, description) in enumerate(DEFAULT_CATEGORIES, start=1):
            FindingCategory.objects.get_or_create(organization=organization, code=code, defaults={
                "name": name, "description": description, "sort_order": order, "is_system": True})
    if not FindingSeverity.objects.filter(organization=organization).exists():
        for code, name, rank, requires_owner, due_days, escalation, default in DEFAULT_SEVERITIES:
            FindingSeverity.objects.get_or_create(organization=organization, code=code, defaults={
                "name": name, "rank": rank, "requires_owner": requires_owner, "default_due_days": due_days,
                "is_escalation": escalation, "is_default": default, "is_system": True})


def _audit(verb: str, instance: Any, actor: Any, description: str, **metadata: Any) -> None:
    log_activity(verb=verb, organization=instance.organization, actor=actor, target=instance, description=description,
                 metadata={"code": instance.code, **metadata})


def _authorize(actor: Any, organization: Any) -> None:
    if not policies.can_manage_config(actor, organization):
        raise PermissionDenied


def _name(value: Any) -> str:
    value = (value or "").strip()
    if not value or len(value) > 100:
        raise FindingError(_("A name of 1 to 100 characters is required."), "invalid_name")
    return value


def _code(value: Any) -> str:
    value = (value or "").strip().lower()
    if not _CODE.match(value):
        raise FindingError(_("The code may use lowercase letters, digits, - and _ (up to 40 characters)."), "invalid_code")
    return value


# -- categories --------------------------------------------------------------------------------------------------------------
@transaction.atomic
def save_category(*, actor: Any, organization: Any, category: FindingCategory | None = None, code: str = "", name: str = "",
                  description: str = "", is_active: bool = True, sort_order: int | None = None) -> FindingCategory:
    _authorize(actor, organization)
    if category is not None and category.organization_id != organization.pk:
        raise PermissionDenied
    created = category is None
    if created:
        category = FindingCategory(organization=organization, code=_code(code))
    elif code and _code(code) != category.code:
        raise FindingError(_("The code of a category never changes."), "code_immutable")
    category.name = _name(name or category.name)
    category.description = (description or "")[:300]
    category.is_active = bool(is_active)
    if sort_order is not None:
        category.sort_order = max(0, int(sort_order))
    stamp_history(category, actor)
    try:
        with transaction.atomic():
            category.save()
    except IntegrityError as exc:
        raise FindingConflict(_("A category with this code already exists."), "duplicate_code") from exc
    _audit(VERBS["category_saved"], category, actor, f"{'Created' if created else 'Updated'} finding category {category.name}", created=created, is_active=category.is_active)
    return category


# -- severities --------------------------------------------------------------------------------------------------------------
@transaction.atomic
def save_severity(*, actor: Any, organization: Any, severity: FindingSeverity | None = None, code: str = "", name: str = "", rank: int | None = None,
                  requires_owner: bool = False, default_due_days: int | None = None, is_escalation: bool = False,
                  is_default: bool = False, is_active: bool = True) -> FindingSeverity:
    _authorize(actor, organization)
    if severity is not None and severity.organization_id != organization.pk:
        raise PermissionDenied
    created = severity is None
    if created:
        severity = FindingSeverity(organization=organization, code=_code(code))
    elif code and _code(code) != severity.code:
        raise FindingError(_("The code of a severity level never changes."), "code_immutable")
    severity.name = _name(name or severity.name)
    if rank is not None:
        if isinstance(rank, bool) or not isinstance(rank, int) or not 1 <= rank <= 100:
            raise FindingError(_("The rank must be a whole number from 1 (least severe) to 100."), "invalid_rank")
        severity.rank = rank
    elif created:
        raise FindingError(_("A rank is required."), "invalid_rank")
    if default_due_days is not None and (isinstance(default_due_days, bool) or not isinstance(default_due_days, int) or not 1 <= default_due_days <= 3650):
        raise FindingError(_("Default due days must be a whole number from 1 to 3650, or empty."), "invalid_due_days")
    severity.default_due_days = default_due_days
    severity.requires_owner, severity.is_escalation = bool(requires_owner), bool(is_escalation)
    if not is_active and severity.is_default and not created:
        raise FindingError(_("The default severity cannot be deactivated; choose another default first."), "default_required")
    severity.is_active = bool(is_active)
    if is_default and not severity.is_active:
        raise FindingError(_("An inactive severity cannot be the default."), "default_required")
    if is_default:
        FindingSeverity.objects.filter(organization=organization, is_default=True).exclude(pk=severity.pk).update(is_default=False)
    elif severity.is_default and not created:
        raise FindingError(_("Make another severity the default instead of unsetting this one."), "default_required")
    severity.is_default = bool(is_default)
    stamp_history(severity, actor)
    try:
        with transaction.atomic():
            severity.save()
    except IntegrityError as exc:
        name_ = getattr(getattr(exc.__cause__, "diag", None), "constraint_name", "") or ""
        raise FindingConflict(_("The code or rank is already used by another severity level.") if "unique" in name_ else _("The severity breaks an integrity rule."),
                              "duplicate_code") from exc
    _audit(VERBS["severity_saved"], severity, actor, f"{'Created' if created else 'Updated'} finding severity {severity.name}", created=created,
           rank=severity.rank, is_active=severity.is_active)
    return severity
