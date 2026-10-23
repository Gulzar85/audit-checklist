"""``create_from_evaluation``: the finding a published *evaluation rule* (Phase 16) asked for.

The Findings engine stays the only place findings are created.  This is the sibling of ``detect_for_execution`` for execution-level rules
(a score threshold, a critical flag) that have no single question to point at: the source is ``evaluation``, the identity is the caller's
``detection_key`` (enforced by the same partial unique constraint), so repeated or concurrent requests return the existing finding.  The
category and severity must be active in the execution's organization - there is no silent fallback (the rule set is validated against them
at publication, and a later deactivation makes the *action* fail visibly, never create a differently classified finding).
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from django.db import IntegrityError, transaction
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from .. import events
from ..constants import SourceType
from ..models import Finding, FindingCategory, FindingSeverity
from .base import VERBS, lock_organization, log, next_number
from .errors import FindingError


def create_from_evaluation(execution: Any, *, rule_code: str, rule_name: str, detection_key: str, category_code: str, severity_code: str, title: str,
                           description: str = "", requires_review: bool = False, actor: Any = None) -> tuple[Finding, bool]:
    """Return ``(finding, created)`` for a *submitted* execution.  Idempotent on ``detection_key``."""
    from apps.executions.constants import ExecutionStatus

    if execution.status != ExecutionStatus.SUBMITTED:
        raise FindingError(_("Findings are requested only for submitted executions."), "not_submitted")
    if not detection_key or not rule_code or not title.strip():
        raise FindingError(_("An evaluation finding needs a rule, a detection key and a title."), "invalid_request")
    organization, assignment = execution.organization, execution.assignment
    with transaction.atomic():
        lock_organization(organization.pk)
        found = Finding.objects.filter(organization=organization, detection_key=detection_key).first()
        if found is not None:
            return found, False
        category = FindingCategory.objects.filter(organization=organization, code=category_code, is_active=True).first()
        severity = FindingSeverity.objects.filter(organization=organization, code=severity_code, is_active=True).first()
        if category is None:
            raise FindingError(_("The category “%(c)s” is not an active category of this organization.") % {"c": category_code}, "invalid_category")
        if severity is None:
            raise FindingError(_("The severity “%(s)s” is not an active severity level of this organization.") % {"s": severity_code}, "invalid_severity")
        detected_at = execution.submitted_at or timezone.now()
        finding = Finding(
            organization=organization, number=next_number(organization.pk), title=title.strip()[:200], description=(description or "").strip()[:2000], category=category,
            severity=severity, source_type=SourceType.EVALUATION, needs_review=bool(requires_review), inherit_evidence=False, location=assignment.location,
            assignment=assignment, execution=execution, version=execution.version, rule_code=rule_code[:64], rule_name=(rule_name or "")[:200],
            detection_key=detection_key[:200], detected_at=detected_at, detected_by=execution.submitted_by or actor,
            due_at=detected_at + timedelta(days=severity.default_due_days) if severity.default_due_days else None,
        )
        try:
            with transaction.atomic():
                finding.save()
        except IntegrityError:
            racer = Finding.objects.filter(organization=organization, detection_key=detection_key).first()
            if racer is None:
                raise
            return racer, False
        log(VERBS["raised"], finding, actor or execution.submitted_by, f"Evaluation rule “{finding.rule_name or finding.rule_code}” requested {finding.reference}: {finding.title}",
            source=SourceType.EVALUATION, rule=rule_code, execution_id=str(execution.pk), severity=severity.code, category=category.code, needs_review=finding.needs_review)
        events.send_on_commit(events.finding_raised, finding, actor or execution.submitted_by, source=SourceType.EVALUATION)
    return finding, True
