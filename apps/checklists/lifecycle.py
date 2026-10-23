"""The checklist version lifecycle: create a draft, validate, publish, supersede, discard, compare, seal.

One module owns every status transition, so no view, serializer, admin action or import decides lifecycle rules on its own.

States and transitions (ADR 0048)::

    DRAFT ──publish──▶ PUBLISHED ──(a newer version is published)──▶ SUPERSEDED
      │
      └──discard──▶ (deleted: a draft was never relied upon)

* ``DRAFT`` is the only editable state, and a template has at most one draft.
* ``PUBLISHED`` is the single *current* version of a template (database-enforced): immutable, assignable.
* ``SUPERSEDED`` is a formerly current version: immutable, readable, never deleted; **not** assignable by default (policy,
  :func:`assert_assignable`), but existing references to it stay valid.  It is the base of a rollback: copy it into a new
  draft and publish that - history is never rewritten.
* Archiving is a property of the *template* (``TemplateStatus.ARCHIVED``), not a fourth version status: one source of truth
  cannot contradict itself, and an archived template simply refuses new drafts, publications and assignments.

Concurrency: every operation first locks the template row (``SELECT ... FOR UPDATE``), the same lock the structure services
take, so publishing, editing and creating drafts of one checklist are serialised and each sees the other's committed result.
The partial unique indexes (one draft, one published per template, unique version numbers) are the safety net behind it.
"""

from __future__ import annotations

from typing import Any

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models import Max, Prefetch
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from simple_history.utils import bulk_create_with_history

from apps.core.services import stamp_history

from . import constants as c
from . import hooks
from .comparison import Comparison, compare_manifests
from .guards import allow_definition_changes
from .manifest import build_manifest, manifest_hash
from .models import ChecklistItem, ChecklistOption, ChecklistRule, ChecklistSection, ChecklistTemplate, ChecklistVersion
from .publication import PublicationReport, validate_for_publication
from .services import VERBS, _lock_template, _log, _touch

#: Legal status transitions.  Anything not listed is refused (``assert_transition``).
ALLOWED_TRANSITIONS: dict[str, frozenset[str]] = {
    c.VersionStatus.DRAFT: frozenset({c.VersionStatus.PUBLISHED}),
    c.VersionStatus.PUBLISHED: frozenset({c.VersionStatus.SUPERSEDED}),
    c.VersionStatus.SUPERSEDED: frozenset(),
}


class LifecycleError(ValidationError):
    """A lifecycle rule refused the operation (``lifecycle_code`` says which)."""

    def __init__(self, message: Any, lifecycle_code: str = "lifecycle_error") -> None:
        super().__init__(message, code=lifecycle_code)
        self.lifecycle_code = lifecycle_code


class ConflictError(LifecycleError):
    """The request raced with another change: a stale draft, an already-published version, a concurrent draft."""


class PublicationBlocked(LifecycleError):
    """The draft has blocking validation errors; ``report`` carries every finding."""

    def __init__(self, report: PublicationReport) -> None:
        super().__init__(report.messages() or [str(_("The draft cannot be published."))], "publication_blocked")
        self.report = report


def assert_transition(old: str, new: str) -> None:
    if new not in ALLOWED_TRANSITIONS.get(old, frozenset()):
        raise LifecycleError(
            _("A %(old)s version cannot become %(new)s.") % {"old": old, "new": new}, "invalid_transition"
        )


def _actor(actor: Any) -> Any:
    return actor if getattr(actor, "is_authenticated", False) else None


# ---------------------------------------------------------------------------
# Cloning
# ---------------------------------------------------------------------------
def clone_definition(source: ChecklistVersion, target: ChecklistVersion, *, actor: Any = None) -> dict[str, int]:
    """Copy sections, items, options and rules of ``source`` into the draft ``target`` with new identifiers.

    Four ``bulk_create`` calls (with history rows) after four reads, whatever the size.  Item, option, section and rule
    **codes and values are preserved** so rules - which reference questions and sections by code - and reports keep
    working; every row is a new, independent row owned by ``target`` (editing the copy never touches the source).
    """
    items_qs = ChecklistItem.objects.order_by("sort_order").prefetch_related(
        Prefetch("options", queryset=ChecklistOption.objects.order_by("sort_order"))
    )
    sections = list(source.sections.order_by("sort_order").prefetch_related(Prefetch("items", queryset=items_qs)))
    rules = list(source.rules.order_by("sort_order"))
    new_sections: list[ChecklistSection] = []
    new_items: list[ChecklistItem] = []
    new_options: list[ChecklistOption] = []
    for section in sections:
        fresh = ChecklistSection(
            version=target, code=section.code, name=section.name, description=section.description,
            instructions=section.instructions, sort_order=section.sort_order, is_required=section.is_required,
        )
        new_sections.append(fresh)
        for item in section.items.all():
            copy = ChecklistItem(
                section=fresh, code=item.code, question=item.question, description=item.description, help_text=item.help_text,
                instructions=item.instructions, answer_type=item.answer_type, config=item.config, metadata=item.metadata,
                is_required=item.is_required, is_active=item.is_active, sort_order=item.sort_order,
            )
            new_items.append(copy)
            new_options.extend(
                ChecklistOption(item=copy, value=o.value, label=o.label, sort_order=o.sort_order, is_active=o.is_active)
                for o in item.options.all()
            )
    new_rules = [
        ChecklistRule(version=target, code=r.code, name=r.name, description=r.description, condition=r.condition,
                      actions=r.actions, sort_order=r.sort_order, is_active=r.is_active)
        for r in rules
    ]
    user, reason = _actor(actor), VERBS["version_cloned"]
    for model, objs in ((ChecklistSection, new_sections), (ChecklistItem, new_items), (ChecklistOption, new_options), (ChecklistRule, new_rules)):
        if objs:
            bulk_create_with_history(objs, model, default_user=user, default_change_reason=reason)
    counts = {"sections": len(new_sections), "items": len(new_items), "options": len(new_options), "rules": len(new_rules)}
    for hook in hooks.clone_hooks:  # extra definition data owned by other apps (e.g. scoring) follows the version
        counts.update(hook(source, target, actor))
    return counts


# ---------------------------------------------------------------------------
# Creating drafts
# ---------------------------------------------------------------------------
def current_published(template: ChecklistTemplate) -> ChecklistVersion | None:
    return template.versions.filter(status=c.VersionStatus.PUBLISHED).first()


@transaction.atomic
def create_draft(template: ChecklistTemplate, *, actor: Any = None, change_summary: str = "", source: ChecklistVersion | None = None) -> ChecklistVersion:
    """Start the next draft as an independent copy of ``source`` (default: the current published version).

    ``source`` may be any non-draft version of this template - copying a historical version is how a rollback is done.
    """
    template = _lock_template(template.pk)
    if template.status == c.TemplateStatus.ARCHIVED:
        raise LifecycleError(_("The checklist is archived and cannot be used to create new versions."), "template_archived")
    if template.versions.filter(status=c.VersionStatus.DRAFT).exists():
        raise LifecycleError(_("This checklist already has a draft version. Publish or discard it first."), "draft_exists")
    if source is None:
        source = current_published(template) or template.versions.order_by("-version_number").first()
    else:
        source = template.versions.filter(pk=source.pk).first()
        if source is None:
            raise LifecycleError(_("The selected version does not belong to this checklist."), "foreign_version")
        if source.status == c.VersionStatus.DRAFT:
            raise LifecycleError(_("A draft cannot be used as the base of another draft."), "invalid_base")
    number = (template.versions.aggregate(top=Max("version_number"))["top"] or 0) + 1
    version = ChecklistVersion(
        template=template, version_number=number, change_summary=(change_summary or "").strip()[:300], created_by=_actor(actor),
        based_on=source,
    )
    stamp_history(version, actor, VERBS["version_created"])
    try:
        with transaction.atomic():
            version.save()
    except IntegrityError as exc:  # a competing request won the number or the single draft slot
        raise ConflictError(_("Another draft was created at the same time. Refresh the version history."), "draft_exists") from exc
    counts = clone_definition(source, version, actor=actor) if source is not None else {}
    _touch(template, actor)
    _log(verb=VERBS["version_created"], organization=template.organization, actor=actor, target=version,
         description=f"Created draft version {number} of {template.code}",
         metadata={"based_on": source.version_number if source else None})
    if source is not None:
        _log(verb=VERBS["version_cloned"], organization=template.organization, actor=actor, target=version,
             description=f"Draft version {number} of {template.code} cloned from version {source.version_number}",
             metadata={"source_version": source.version_number, "source_status": source.status, **counts})
    return version


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------
def _fresh(version: ChecklistVersion) -> ChecklistVersion:
    return ChecklistVersion.objects.select_related("template__organization", "based_on").get(pk=version.pk)


def run_validation(version: ChecklistVersion, *, actor: Any = None) -> PublicationReport:
    """The explicit "Validate" action: the same report publishing uses, plus an audit entry that a validation was run."""
    version = _fresh(version)
    report = validate_for_publication(version)
    template = version.template
    verb = VERBS["version_validated"] if report.ok else VERBS["version_validation_failed"]
    _log(verb=verb, organization=template.organization, actor=actor, target=version,
         description=f"Validated {template.code} v{version.version_number}: {len(report.errors)} error(s), {len(report.warnings)} warning(s)",
         metadata={"errors": len(report.errors), "warnings": len(report.warnings), "codes": sorted({f.code for f in report.errors})[:30]})
    return report


# ---------------------------------------------------------------------------
# Publishing
# ---------------------------------------------------------------------------
def publish_version(version: ChecklistVersion, *, actor: Any = None, change_summary: str | None = None, expected_hash: str | None = None) -> ChecklistVersion:
    """Publish a draft.  Transactional, serialised per template, validated, and safe to repeat.

    * ``expected_hash`` (optional) is the ``draft_hash`` the caller last saw; if the draft changed since, the publication
      is refused with a :class:`ConflictError` instead of publishing something nobody reviewed.
    * Publishing an already published version raises :class:`ConflictError` (``already_published``) and changes nothing,
      so a double click or a retried request cannot produce a second publication event.
    """
    try:
        return _publish(version, actor=actor, change_summary=change_summary, expected_hash=expected_hash)
    except PublicationBlocked as exc:  # outside the transaction, so the audit entry survives the refusal
        template = ChecklistTemplate.objects.select_related("organization").get(pk=version.template_id)
        _log_blocked(template, version, actor, exc.report)
        raise


def _log_blocked(template: ChecklistTemplate, version: ChecklistVersion, actor: Any, report: PublicationReport) -> None:
    errors = report.errors
    _log(template=template, verb=VERBS["version_validation_failed"], organization=template.organization, actor=actor, target=version,
         description=f"Publication of {template.code} v{version.version_number} refused: {len(errors)} error(s)",
         metadata={"errors": len(errors), "codes": sorted({f.code for f in errors})[:30]})
    rule_errors = [f for f in errors if f.subject.startswith("rule:")]
    if rule_errors:  # the Phase 6 audit trail of rule problems keeps working
        _log(template=template, verb=VERBS["rule_validation_failed"], organization=template.organization, actor=actor, target=version,
             description=f"Rule validation failed while publishing on {template.code} v{version.version_number}",
             metadata={"count": len(rule_errors), "issues": [{"rule": f.subject[5:], "code": f.code, "path": ""} for f in rule_errors[:20]]})


@transaction.atomic
def _publish(version: ChecklistVersion, *, actor: Any, change_summary: str | None, expected_hash: str | None) -> ChecklistVersion:
    template = _lock_template(version.template_id)
    locked = ChecklistVersion.objects.select_for_update(of=("self",)).select_related("template__organization", "based_on").filter(
        pk=version.pk, template_id=template.pk
    ).first()
    if locked is None:
        raise LifecycleError(_("The version does not belong to this checklist."), "foreign_version")
    if locked.status != c.VersionStatus.DRAFT:
        raise ConflictError(
            _("Version %(n)s is already published; nothing was changed.") % {"n": locked.version_number}, "already_published"
        )
    assert_transition(locked.status, c.VersionStatus.PUBLISHED)
    if change_summary is not None:
        locked.change_summary = change_summary.strip()[:300]  # part of what is validated (warns when empty)
    report = validate_for_publication(locked)
    if report.errors:
        raise PublicationBlocked(report)
    if expected_hash and expected_hash != report.draft_hash:
        raise ConflictError(
            _("The draft changed after it was last validated. Review the changes and validate again before publishing."), "stale_draft"
        )
    now = timezone.now()
    superseded: list[int] = []
    for old in ChecklistVersion.objects.select_for_update().filter(template=template, status=c.VersionStatus.PUBLISHED).exclude(pk=locked.pk):
        assert_transition(old.status, c.VersionStatus.SUPERSEDED)
        old.status, old.superseded_at = c.VersionStatus.SUPERSEDED, now
        stamp_history(old, actor, VERBS["version_superseded"])
        old.save()
        superseded.append(old.version_number)
    locked.status, locked.published_at, locked.published_by = c.VersionStatus.PUBLISHED, now, _actor(actor)
    locked.integrity_hash = report.draft_hash
    locked.publication_report = {
        "validator": 1, "validated_at": now.isoformat(), "stats": report.stats,
        "warnings": [f.as_dict() for f in report.warnings[:50]],
    }
    stamp_history(locked, actor, VERBS["version_published"])
    locked.save()
    _touch(template, actor)
    _log(verb=VERBS["version_published"], organization=template.organization, actor=actor, target=locked,
         description=f"Published {template.code} v{locked.version_number}",
         metadata={"version": locked.version_number, "warnings": len(report.warnings), "integrity_hash": report.draft_hash, **report.stats})
    for number in superseded:
        _log(verb=VERBS["version_superseded"], organization=template.organization, actor=actor, target=locked,
             description=f"{template.code} v{number} was superseded by v{locked.version_number}",
             metadata={"superseded_version": number, "by_version": locked.version_number})
    return locked


# ---------------------------------------------------------------------------
# Discarding a draft
# ---------------------------------------------------------------------------
def protected_dependencies(version: ChecklistVersion) -> list[str]:
    """What forbids deleting ``version`` (empty today: a draft cannot be assigned or executed).

    Later phases add their own reasons here (for example an assignment that references the version), so discarding stays a
    single, centrally-checked operation.
    """
    return []


@transaction.atomic
def discard_draft(version: ChecklistVersion, *, actor: Any = None) -> None:
    """Delete a draft that was never published.  Published and superseded versions are never deleted."""
    template = _lock_template(version.template_id)
    locked = ChecklistVersion.objects.select_for_update().filter(pk=version.pk, template_id=template.pk).first()
    if locked is None:
        raise LifecycleError(_("The version does not belong to this checklist."), "foreign_version")
    if locked.status != c.VersionStatus.DRAFT:
        raise LifecycleError(_("Only a draft can be discarded; published versions are kept for history."), "not_draft")
    if template.status == c.TemplateStatus.ARCHIVED:
        raise LifecycleError(_("An archived checklist cannot be changed. Restore it first."), "template_archived")
    if not template.versions.exclude(pk=locked.pk).exists():
        raise LifecycleError(_("This is the only version of the checklist. Delete the draft checklist instead."), "only_version")
    blockers = protected_dependencies(locked)
    if blockers:
        raise LifecycleError(blockers, "protected")
    number, based_on = locked.version_number, locked.based_on.version_number if locked.based_on_id else None
    counts = {"sections": locked.sections.count(), "rules": locked.rules.count()}
    stamp_history(locked, actor, VERBS["version_discarded"])
    locked.delete()
    _touch(template, actor)
    _log(template=template, verb=VERBS["version_discarded"], organization=template.organization, actor=actor,
         description=f"Discarded draft version {number} of {template.code}", metadata={"version": number, "based_on": based_on, **counts})


# ---------------------------------------------------------------------------
# Comparison
# ---------------------------------------------------------------------------
def compare_versions(template: ChecklistTemplate, old: ChecklistVersion, new: ChecklistVersion, *, actor: Any = None, log: bool = True) -> Comparison:
    """Compare two versions of ``template`` (read-only; never alters either)."""
    if old.template_id != template.pk or new.template_id != template.pk:
        raise LifecycleError(_("The selected versions do not belong to the same checklist template."), "foreign_version")
    comparison = compare_manifests(build_manifest(old), build_manifest(new))
    if log:
        _log(template=template, verb=VERBS["version_compared"], organization=template.organization, actor=actor, target=new,
             description=f"Compared {template.code} v{old.version_number} with v{new.version_number}",
             metadata={"from": old.version_number, "to": new.version_number, "changes": comparison.counts()["total"]})
    return comparison


# ---------------------------------------------------------------------------
# Assignability (the contract Phase 8 builds on)
# ---------------------------------------------------------------------------
def assert_assignable(version: ChecklistVersion) -> None:
    """Raise unless ``version`` may be used for a *new* assignment: the current published version of an active template.

    Superseded versions stay readable and existing assignments to them keep working, but nothing new is created from them.
    """
    version = _fresh(version)  # never trust a cached status: assignability is decided on the stored state
    template = version.template
    if template.status == c.TemplateStatus.ARCHIVED:
        raise LifecycleError(_("The checklist is archived and cannot be assigned."), "template_archived")
    if template.status != c.TemplateStatus.ACTIVE:
        raise LifecycleError(_("Activate the checklist before assigning it."), "template_not_active")
    if version.status == c.VersionStatus.SUPERSEDED:
        raise LifecycleError(_("This version was superseded by a newer one and cannot be assigned."), "superseded")
    if version.status != c.VersionStatus.PUBLISHED:
        raise LifecycleError(_("Only a published version can be assigned."), "not_published")
    if not template.organization.is_active:
        raise LifecycleError(_("This organization is not active."), "organization_inactive")


def is_assignable(version: ChecklistVersion) -> bool:
    try:
        assert_assignable(version)
    except ValidationError:
        return False
    return True


# ---------------------------------------------------------------------------
# Sealing legacy versions (maintenance)
# ---------------------------------------------------------------------------
def seal_version(version: ChecklistVersion, *, actor: Any = None) -> bool:
    """Compute and store the integrity hash of a published/superseded version that has none (published before hashes existed).

    The only code path that writes a non-draft version, so it runs inside the explicit, logged maintenance override.
    Returns ``True`` when a hash was written.
    """
    version = _fresh(version)
    if version.status == c.VersionStatus.DRAFT or version.integrity_hash:
        return False
    digest = manifest_hash(version)
    with allow_definition_changes():
        ChecklistVersion.objects.filter(pk=version.pk, integrity_hash="").update(integrity_hash=digest)
    _log(template=version.template, verb=VERBS["version_sealed"], organization=version.template.organization, actor=actor, target=version,
         description=f"Sealed {version.template.code} v{version.version_number} (integrity hash recorded)",
         metadata={"version": version.version_number, "integrity_hash": digest})
    return True
