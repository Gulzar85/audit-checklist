"""Business operations for checklist definitions.

Every write - from the builder UI, the API, imports and the admin actions - goes through here, so
validation, locking, version safety, history and the activity log apply uniformly.  Callers authorize
first (``apps.checklists.policies``); services enforce the *data* rules that must hold for every writer:

* a **published or superseded version is immutable** - structure changes are refused, create a new draft version instead
  (the lifecycle - draft, validate, publish, supersede, discard, compare - lives in ``lifecycle.py``; the services below
  keep the original entry points and delegate);
* an archived template, or one of an inactive organization, cannot be edited;
* codes are normalised and stable (a template's ``code`` and ``slug`` never change; an item's ``code`` is
  changed only deliberately);
* sort orders are explicit, contiguous integers, rewritten atomically (the unique constraints are deferred).

Concurrency: structure writes first lock the template row, so two builders editing one checklist are
serialised and each validation sees the other's committed result.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models import Max
from django.utils import timezone
from django.utils.text import slugify
from django.utils.translation import gettext_lazy as _

from apps.core.services import apply_changes, log_activity, stamp_history
from apps.locations.models import normalize_code, validate_code

from . import constants as c
from .metadata import clean_metadata
from .rules import Issue, RuleSpec, snapshot_for_version, validate_rule_set
from .models import (
    ChecklistCategory,
    ChecklistItem,
    ChecklistOption,
    ChecklistRule,
    ChecklistSection,
    ChecklistTemplate,
    ChecklistVersion,
)

VERBS = {
    "template_created": "checklist.created",
    "template_updated": "checklist.updated",
    "template_activated": "checklist.activated",
    "template_archived": "checklist.archived",
    "template_restored": "checklist.restored",
    "template_duplicated": "checklist.duplicated",
    "template_deleted": "checklist.deleted",
    "version_created": "checklist_version.created",
    "version_published": "checklist_version.published",
    "version_validated": "checklist_version.validated",
    "version_validation_failed": "checklist_version.validation_failed",
    "version_cloned": "checklist_version.cloned",
    "version_superseded": "checklist_version.superseded",
    "version_discarded": "checklist_version.discarded",
    "version_compared": "checklist_version.compared",
    "version_sealed": "checklist_version.sealed",
    "section_added": "checklist_section.added",
    "section_updated": "checklist_section.updated",
    "section_removed": "checklist_section.removed",
    "sections_reordered": "checklist_section.reordered",
    "item_added": "checklist_item.added",
    "item_updated": "checklist_item.updated",
    "item_removed": "checklist_item.removed",
    "items_reordered": "checklist_item.reordered",
    "item_type_changed": "checklist_item.answer_type_changed",
    "item_config_changed": "checklist_item.config_changed",
    "options_synced": "checklist_option.synced",
    "option_added": "checklist_option.added",
    "option_updated": "checklist_option.updated",
    "option_removed": "checklist_option.removed",
    "options_reordered": "checklist_option.reordered",
    "rule_created": "checklist_rule.created",
    "rule_updated": "checklist_rule.updated",
    "rule_enabled": "checklist_rule.enabled",
    "rule_disabled": "checklist_rule.disabled",
    "rule_condition_changed": "checklist_rule.condition_changed",
    "rule_actions_changed": "checklist_rule.actions_changed",
    "rule_removed": "checklist_rule.removed",
    "rules_reordered": "checklist_rule.reordered",
    "rule_validation_failed": "checklist_rule.validation_failed",
    "category_created": "checklist_category.created",
}

_VALUE_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,63}$")


def _template_id(target):
    """The owning template of any checklist object, so a template's activity tab can find its events."""
    if isinstance(target, ChecklistTemplate):
        return str(target.pk)
    for path in ("template_id", "version.template_id", "section.version.template_id", "item.section.version.template_id"):
        obj = target
        try:
            for part in path.split("."):
                obj = getattr(obj, part)
        except AttributeError:
            continue
        return str(obj)
    return None


def _log(*, template=None, target=None, metadata=None, **kwargs):
    """``log_activity`` plus ``metadata.template_id`` (for the template's activity feed)."""
    template_id = str(template.pk) if template is not None else _template_id(target)
    meta = dict(metadata or {})
    if template_id:
        meta["template_id"] = template_id
    return log_activity(target=target, metadata=meta, **kwargs)


# ---------------------------------------------------------------------------
# Guards
# ---------------------------------------------------------------------------
def _lock_template(template_id: Any) -> ChecklistTemplate:
    template = ChecklistTemplate.objects.select_for_update().select_related("organization").get(pk=template_id)
    if not template.organization.is_active:
        raise ValidationError(_("This organization is not active, so its checklists cannot be changed."))
    return template


def _editable(version: ChecklistVersion) -> tuple[ChecklistTemplate, ChecklistVersion]:
    """Lock the template and re-read the version; refuse anything but a draft of a live template."""
    template = _lock_template(version.template_id)
    fresh = ChecklistVersion.objects.get(pk=version.pk)
    if fresh.status != c.VersionStatus.DRAFT:
        raise ValidationError(
            _("Version %(n)s is already published and cannot be edited. Create a new draft version to make changes.")
            % {"n": fresh.version_number},
            code="version_immutable",
        )
    if template.status == c.TemplateStatus.ARCHIVED:
        raise ValidationError(_("An archived checklist cannot be changed. Restore it first."), code="template_archived")
    return template, fresh


def _next_code(prefix: str, existing: Iterable[str]) -> str:
    """``S-004``-style code after the highest existing ``<prefix>-NNN``."""
    numbers = [int(m.group(1)) for code in existing if (m := re.fullmatch(rf"{prefix}-(\d+)", code or ""))]
    return f"{prefix}-{(max(numbers) if numbers else 0) + 1:03d}"


class RuleValidationError(ValidationError):
    """A rule (or the rule set) failed validation; ``issues`` carries every finding with its rule code and path."""

    def __init__(self, issues: list[Issue], prefix: str = "", show_rule: bool = False) -> None:
        self.issues = issues
        grouped: dict[str, list[str]] = {}
        for issue in issues:
            key = "condition" if issue.path.startswith("condition") else "actions" if issue.path.startswith("actions") else "__all__"
            where = f"Rule {issue.rule}: " if issue.rule and (show_rule or key == "__all__") else ""
            grouped.setdefault(key, []).append(f"{prefix}{where}{issue.message}")
        super().__init__(grouped)


def _log_rule_failure(template: ChecklistTemplate, version: ChecklistVersion, actor: Any, issues: list[Issue], context: str) -> None:
    """Record that a rule change/publish was refused (no user-entered text is stored, only codes and paths)."""
    _log(
        template=template, verb=VERBS["rule_validation_failed"], organization=template.organization, actor=actor, target=version,
        description=f"Rule validation failed while {context} on {template.code} v{version.version_number}",
        metadata={"count": len(issues), "issues": [{"rule": i.rule, "code": i.code, "path": i.path} for i in issues[:20]]},
    )


def _assert_rules_valid(version: ChecklistVersion, what: str) -> None:
    """Refuse a structure change (inside its transaction) that would leave the version's rules invalid."""
    if not ChecklistRule.objects.filter(version=version).exists():
        return
    report = validate_rule_set(snapshot_for_version(version))
    if report.errors:
        raise ValidationError(
            [_("%(what)s would break the rules of this version. Update or remove the rule first. ") % {"what": what} + e.message
             for e in report.errors[:3]]
        )


def _touch(template: ChecklistTemplate, actor: Any) -> None:
    template.updated_by = actor if getattr(actor, "is_authenticated", False) else None
    template.save(update_fields=["updated_by", "updated_at"])


def _next_order(queryset: Any) -> int:
    return (queryset.aggregate(top=Max("sort_order"))["top"] or 0) + 1


def _db_error(exc: IntegrityError) -> ValidationError:
    name = getattr(getattr(exc.__cause__, "diag", None), "constraint_name", "") or ""
    messages = {
        "unique_checklist_code_per_organization": {"code": _("A checklist with this code already exists in this organization.")},
        "unique_checklist_slug_per_organization": {"name": _("A checklist with this name already exists.")},
        "unique_item_code_per_section": {"code": _("This code is already used in this section.")},
        "unique_option_value_per_item": {"value": _("This option value already exists for the item.")},
        "unique_checklist_category_per_organization": {"name": _("This category already exists.")},
        "one_draft_version_per_template": {"__all__": _("This checklist already has a draft version.")},
    }
    return ValidationError(messages.get(name, _("The change conflicts with the current checklist. Reload and try again.")))


def _slug(organization: Any, name: str) -> str:
    base = slugify(name)[:70] or "checklist"
    slug, n = base, 2
    while ChecklistTemplate.objects.filter(organization=organization, slug=slug).exists():
        slug = f"{base}-{n}"
        n += 1
    return slug


# ---------------------------------------------------------------------------
# Categories
# ---------------------------------------------------------------------------
@transaction.atomic
def create_category(*, organization: Any, name: str, actor: Any = None) -> ChecklistCategory:
    name = (name or "").strip()
    if not name:
        raise ValidationError({"name": _("A category needs a name.")})
    existing = ChecklistCategory.objects.filter(organization=organization, name__iexact=name).first()
    if existing:
        return existing
    category = ChecklistCategory(organization=organization, name=name, sort_order=_next_order(ChecklistCategory.objects.filter(organization=organization)))
    stamp_history(category, actor, VERBS["category_created"])
    category.save()
    _log(verb=VERBS["category_created"], organization=organization, actor=actor, target=category,
                 description=f"Created checklist category {name}")
    return category


# ---------------------------------------------------------------------------
# Templates
# ---------------------------------------------------------------------------
def _new_draft_version(template: ChecklistTemplate, actor: Any, summary: str = "") -> ChecklistVersion:
    number = (template.versions.aggregate(top=Max("version_number"))["top"] or 0) + 1
    version = ChecklistVersion(template=template, version_number=number, change_summary=summary,
                               created_by=actor if getattr(actor, "is_authenticated", False) else None)
    stamp_history(version, actor, VERBS["version_created"])
    version.save()
    return version


@transaction.atomic
def create_checklist_template(
    *, organization: Any, name: str, code: str, actor: Any = None, description: str = "", instructions: str = "",
    estimated_duration_minutes: int | None = None, category: ChecklistCategory | None = None,
) -> ChecklistTemplate:
    """Create a template with an empty draft version 1."""
    if not organization.is_active:
        raise ValidationError(_("This organization is not active, so checklists cannot be created."))
    template = ChecklistTemplate(
        organization=organization, name=(name or "").strip(), code=code, description=description,
        instructions=instructions, estimated_duration_minutes=estimated_duration_minutes, category=category,
        slug=_slug(organization, name or "checklist"),
        created_by=actor if getattr(actor, "is_authenticated", False) else None,
        updated_by=actor if getattr(actor, "is_authenticated", False) else None,
    )
    template.full_clean(exclude=["slug"], validate_unique=False)
    if not template.name:
        raise ValidationError({"name": _("A checklist needs a name.")})
    stamp_history(template, actor, VERBS["template_created"])
    try:
        with transaction.atomic():
            template.save()
    except IntegrityError as exc:
        raise _db_error(exc) from exc
    _new_draft_version(template, actor)
    _log(verb=VERBS["template_created"], organization=organization, actor=actor, target=template,
                 description=f"Created checklist {template.code} {template.name}", metadata={"code": template.code})
    return template


@transaction.atomic
def update_checklist_template(template: ChecklistTemplate, *, actor: Any = None, **data: Any) -> ChecklistTemplate:
    """Update descriptive fields.  ``code`` and ``slug`` are deliberately not editable."""
    template = _lock_template(template.pk)
    if template.status == c.TemplateStatus.ARCHIVED:
        raise ValidationError(_("An archived checklist cannot be changed. Restore it first."))
    allowed = {k: v for k, v in data.items()
               if k in ("name", "description", "instructions", "estimated_duration_minutes", "category")}
    changes = apply_changes(template, allowed)
    if changes:
        template.updated_by = actor if getattr(actor, "is_authenticated", False) else None
        template.full_clean(exclude=["slug"], validate_unique=False)
        stamp_history(template, actor, VERBS["template_updated"])
        template.save()
        _log(verb=VERBS["template_updated"], organization=template.organization, actor=actor, target=template,
                     description=f"Updated checklist {template.code}", metadata={"changes": changes})
    return template


@transaction.atomic
def activate_checklist_template(template: ChecklistTemplate, *, actor: Any = None) -> ChecklistTemplate:
    """Draft -> active.  An active checklist needs at least one published version."""
    template = _lock_template(template.pk)
    if template.status == c.TemplateStatus.ACTIVE:
        return template
    if template.status == c.TemplateStatus.ARCHIVED:
        raise ValidationError(_("Restore the archived checklist first."))
    if not template.versions.filter(status=c.VersionStatus.PUBLISHED).exists():
        raise ValidationError(_("Publish a version before activating the checklist."))
    template.status = c.TemplateStatus.ACTIVE
    template.updated_by = actor if getattr(actor, "is_authenticated", False) else None
    stamp_history(template, actor, VERBS["template_activated"])
    template.save()
    _log(verb=VERBS["template_activated"], organization=template.organization, actor=actor, target=template,
                 description=f"Activated checklist {template.code}")
    return template


@transaction.atomic
def archive_checklist_template(template: ChecklistTemplate, *, actor: Any = None) -> ChecklistTemplate:
    """Archive (never delete): kept for history, hidden from normal selection."""
    template = _lock_template(template.pk)
    if template.status != c.TemplateStatus.ARCHIVED:
        template.status = c.TemplateStatus.ARCHIVED
        template.updated_by = actor if getattr(actor, "is_authenticated", False) else None
        stamp_history(template, actor, VERBS["template_archived"])
        template.save()
        _log(verb=VERBS["template_archived"], organization=template.organization, actor=actor, target=template,
                     description=f"Archived checklist {template.code}")
    return template


@transaction.atomic
def restore_checklist_template(template: ChecklistTemplate, *, actor: Any = None) -> ChecklistTemplate:
    """Archived -> active when it has a published version, otherwise draft."""
    template = _lock_template(template.pk)
    if template.status == c.TemplateStatus.ARCHIVED:
        published = template.versions.filter(status=c.VersionStatus.PUBLISHED).exists()
        template.status = c.TemplateStatus.ACTIVE if published else c.TemplateStatus.DRAFT
        template.updated_by = actor if getattr(actor, "is_authenticated", False) else None
        stamp_history(template, actor, VERBS["template_restored"])
        template.save()
        _log(verb=VERBS["template_restored"], organization=template.organization, actor=actor, target=template,
                     description=f"Restored checklist {template.code}")
    return template


@transaction.atomic
def delete_checklist_template(template: ChecklistTemplate, *, actor: Any = None) -> None:
    """Delete only a draft template that was never published (nothing can reference it)."""
    template = _lock_template(template.pk)
    if template.status != c.TemplateStatus.DRAFT or template.versions.exclude(status=c.VersionStatus.DRAFT).exists():
        raise ValidationError(_("Only a draft checklist that was never published can be deleted. Archive it instead."))
    label, organization = f"{template.code} {template.name}", template.organization
    stamp_history(template, actor, VERBS["template_deleted"])
    # Versions are PROTECTed by the template: remove them explicitly first.
    for version in list(template.versions.all()):
        stamp_history(version, actor, VERBS["template_deleted"])
        version.delete()
    template.delete()
    _log(verb=VERBS["template_deleted"], organization=organization, actor=actor,
                 description=f"Deleted draft checklist {label}")


def _clone_structure(source: ChecklistVersion, target: ChecklistVersion, actor: Any) -> None:
    """Copy sections, items, options and rules of ``source`` into ``target`` (see ``lifecycle.clone_definition``)."""
    from .lifecycle import clone_definition

    clone_definition(source, target, actor=actor)


@transaction.atomic
def duplicate_checklist_template(
    template: ChecklistTemplate, *, actor: Any = None, name: str | None = None, code: str | None = None
) -> ChecklistTemplate:
    """Copy a template (same organization) as a new draft with fresh identifiers and v1 = a copy of the latest version."""
    source = ChecklistTemplate.objects.select_related("organization").get(pk=template.pk)
    new_name = (name or f"{source.name} - Copy").strip()
    base = normalize_code(code or f"{source.code}-COPY")
    new_code, n = base, 2
    while ChecklistTemplate.objects.filter(organization=source.organization, code=new_code).exists():
        new_code = f"{base}-{n}"
        n += 1
    copy = create_checklist_template(
        organization=source.organization, name=new_name, code=new_code, actor=actor, description=source.description,
        instructions=source.instructions, estimated_duration_minutes=source.estimated_duration_minutes,
        category=source.category,
    )
    latest = source.versions.order_by("-version_number").first()
    if latest is not None:
        _clone_structure(latest, copy.versions.get(), actor)
    _log(verb=VERBS["template_duplicated"], organization=source.organization, actor=actor, target=copy,
                 description=f"Duplicated checklist {source.code} as {copy.code}",
                 metadata={"source_id": str(source.pk)})
    return copy


# ---------------------------------------------------------------------------
# Versions
# ---------------------------------------------------------------------------
def create_checklist_version(template: ChecklistTemplate, *, actor: Any = None, change_summary: str = "", source: ChecklistVersion | None = None) -> ChecklistVersion:
    """Start the next draft as an independent copy of the current published version (or of ``source``).

    Refused while a draft exists or when the template is archived; see ``lifecycle.create_draft``.
    """
    from .lifecycle import create_draft

    return create_draft(template, actor=actor, change_summary=change_summary, source=source)


def validate_version_complete(version: ChecklistVersion) -> list[str]:
    """Problems that stop a version being published (empty list = publishable); the blocking errors of the publication report."""
    from .publication import validate_for_publication

    return validate_for_publication(ChecklistVersion.objects.select_related("template__organization", "based_on").get(pk=version.pk)).messages()


def publish_checklist_version(version: ChecklistVersion, *, actor: Any = None, change_summary: str | None = None, expected_hash: str | None = None) -> ChecklistVersion:
    """Publish a draft: validated, atomic, serialised per template; the previous published version is superseded.

    Raises ``lifecycle.PublicationBlocked`` (a ``ValidationError``) for blocking problems and ``lifecycle.ConflictError`` for a
    stale draft or an already published version.  See ``lifecycle.publish_version``.
    """
    from .lifecycle import publish_version

    return publish_version(version, actor=actor, change_summary=change_summary, expected_hash=expected_hash)


# ---------------------------------------------------------------------------
# Sections
# ---------------------------------------------------------------------------
@transaction.atomic
def create_section(version: ChecklistVersion, *, name: str, actor: Any = None, description: str = "",
                   instructions: str = "", is_required: bool = True, code: str = "") -> ChecklistSection:
    template, version = _editable(version)
    name = (name or "").strip()
    if not name:
        raise ValidationError({"name": _("A section needs a name.")})
    existing = list(version.sections.values_list("code", flat=True))
    code = normalize_code(code) or _next_code("S", existing)
    validate_code(code)
    if code in existing:
        raise ValidationError({"code": _("This section code is already used in this checklist version.")})
    section = ChecklistSection(version=version, code=code, name=name, description=description, instructions=instructions,
                               is_required=is_required, sort_order=_next_order(version.sections))
    stamp_history(section, actor, VERBS["section_added"])
    section.save()
    _touch(template, actor)
    _log(verb=VERBS["section_added"], organization=template.organization, actor=actor, target=section,
                 description=f"Added section “{name}” to {template.code}")
    return section


@transaction.atomic
def update_section(section: ChecklistSection, *, actor: Any = None, **data: Any) -> ChecklistSection:
    template, _version = _editable(section.version)
    section = ChecklistSection.objects.get(pk=section.pk)
    allowed = {k: v for k, v in data.items() if k in ("name", "description", "instructions", "is_required")}
    if "name" in allowed and not (allowed["name"] or "").strip():
        raise ValidationError({"name": _("A section needs a name.")})
    changes = apply_changes(section, allowed)
    if changes:
        stamp_history(section, actor, VERBS["section_updated"])
        section.save()
        _touch(template, actor)
        _log(verb=VERBS["section_updated"], organization=template.organization, actor=actor, target=section,
                     description=f"Updated section “{section.name}”", metadata={"changes": changes})
    return section


@transaction.atomic
def delete_section(section: ChecklistSection, *, actor: Any = None) -> None:
    template, version = _editable(section.version)
    section = ChecklistSection.objects.get(pk=section.pk)
    name = section.name
    stamp_history(section, actor, VERBS["section_removed"])
    section.delete()
    _renumber(version.sections.order_by("sort_order"))
    _assert_rules_valid(version, f"Removing the section “{name}”")
    _touch(template, actor)
    _log(template=template, verb=VERBS["section_removed"], organization=template.organization, actor=actor,
                 description=f"Removed section “{name}” from {template.code}")


def _renumber(ordered: Iterable[Any]) -> None:
    """Rewrite sort_order to 1..n in the given order (deferred unique constraints allow the shuffle)."""
    for position, obj in enumerate(ordered, start=1):
        if obj.sort_order != position:
            obj.sort_order = position
            obj.save(update_fields=["sort_order", "updated_at"])


def _apply_order(queryset: Any, ordered_ids: Iterable[Any], what: str) -> list[Any]:
    objs = {str(o.pk): o for o in queryset}
    wanted = [str(i) for i in ordered_ids]
    if sorted(wanted) != sorted(objs) or len(set(wanted)) != len(wanted):
        raise ValidationError(_("The new %(what)s order must list every existing %(what)s exactly once.") % {"what": what})
    return [objs[i] for i in wanted]


@transaction.atomic
def reorder_sections(version: ChecklistVersion, ordered_ids: Iterable[Any], *, actor: Any = None) -> None:
    template, version = _editable(version)
    ordered = _apply_order(version.sections.select_for_update(), ordered_ids, "section")
    _renumber(ordered)
    _touch(template, actor)
    _log(verb=VERBS["sections_reordered"], organization=template.organization, actor=actor, target=version,
                 description=f"Reordered sections of {template.code}")


def _moved(ids: list[Any], target: Any, direction: str) -> list[Any]:
    index = ids.index(target)
    swap = index - 1 if direction == "up" else index + 1
    if direction not in ("up", "down"):
        raise ValidationError(_("Unknown direction."))
    if 0 <= swap < len(ids):
        ids[index], ids[swap] = ids[swap], ids[index]
    return ids


def move_section(section: ChecklistSection, direction: str, *, actor: Any = None) -> None:
    ids = list(section.version.sections.order_by("sort_order").values_list("pk", flat=True))
    reorder_sections(section.version, _moved(ids, section.pk, direction), actor=actor)


# ---------------------------------------------------------------------------
# Items and options
# ---------------------------------------------------------------------------
def _version_codes(version: ChecklistVersion, exclude: Any = None) -> set[str]:
    qs = ChecklistItem.objects.filter(section__version=version)
    if exclude is not None:
        qs = qs.exclude(pk=exclude)
    return set(qs.values_list("code", flat=True))


def _generate_code(version: ChecklistVersion) -> str:
    used = _version_codes(version)
    n = len(used) + 1
    while f"Q-{n:03d}" in used:
        n += 1
    return f"Q-{n:03d}"


def _sync_fixed_options(item: ChecklistItem) -> None:
    """Make the options of a fixed-option type (yes/no, yes/no/n/a) match the platform definition."""
    fixed = c.FIXED_OPTIONS.get(item.answer_type)
    if fixed is None:
        if item.answer_type not in c.CUSTOM_OPTIONS:
            item.options.all().delete()
        return
    wanted = [value for value, _label in fixed]
    if list(item.options.order_by("sort_order").values_list("value", flat=True)) == wanted:
        return
    item.options.all().delete()
    ChecklistOption.objects.bulk_create(
        [ChecklistOption(item=item, value=v, label=lbl, sort_order=i) for i, (v, lbl) in enumerate(fixed, start=1)]
    )


@transaction.atomic
def create_item(
    section: ChecklistSection, *, question: str, actor: Any = None, answer_type: str = c.AnswerType.YES_NO,
    code: str = "", config: Any = None, description: str = "", help_text: str = "", instructions: str = "",
    is_required: bool = True, is_active: bool = True, options: Iterable[tuple[str, str]] = (),
    metadata: Any = None,
) -> ChecklistItem:
    """Add an item to a section.  ``options`` (``(value, label)``) apply to choice types only."""
    template, version = _editable(section.version)
    section = ChecklistSection.objects.get(pk=section.pk)
    question = (question or "").strip()
    if not question:
        raise ValidationError({"question": _("A question is required.")})
    if answer_type not in c.AnswerType.values:
        raise ValidationError({"answer_type": _("Unknown answer type.")})
    code = normalize_code(code) or _generate_code(version)
    validate_code(code)
    if code in _version_codes(version):
        raise ValidationError({"code": _("This code is already used in this checklist version.")})
    item = ChecklistItem(
        section=section, code=code, question=question, description=description, help_text=help_text,
        instructions=instructions, answer_type=answer_type, config=c.clean_config(answer_type, config),
        is_required=is_required, is_active=is_active, sort_order=_next_order(section.items),
        metadata=clean_metadata(metadata),
    )
    stamp_history(item, actor, VERBS["item_added"])
    try:
        with transaction.atomic():
            item.save()
    except IntegrityError as exc:
        raise _db_error(exc) from exc
    _sync_fixed_options(item)
    if answer_type in c.CUSTOM_OPTIONS:
        for value, label in parse_option_entries(options):
            create_option(item, value=value, label=label, actor=actor, log=False)
    _touch(template, actor)
    _log(verb=VERBS["item_added"], organization=template.organization, actor=actor, target=item,
                 description=f"Added item {item.code} to {template.code}",
                 metadata={"answer_type": answer_type})
    return item


@transaction.atomic
def update_item(item: ChecklistItem, *, actor: Any = None, **data: Any) -> ChecklistItem:
    template, version = _editable(item.section.version)
    item = ChecklistItem.objects.select_related("section").get(pk=item.pk)
    allowed = {k: v for k, v in data.items() if k in (
        "question", "code", "answer_type", "config", "description", "help_text", "instructions", "is_required", "is_active",
        "metadata")}
    if "metadata" in allowed:
        allowed["metadata"] = clean_metadata(allowed["metadata"])
    if "question" in allowed and not (allowed["question"] or "").strip():
        raise ValidationError({"question": _("A question is required.")})
    if "code" in allowed:
        allowed["code"] = normalize_code(allowed["code"])
        validate_code(allowed["code"])
        if allowed["code"] in _version_codes(version, exclude=item.pk):
            raise ValidationError({"code": _("This code is already used in this checklist version.")})
    answer_type = allowed.get("answer_type", item.answer_type)
    if answer_type not in c.AnswerType.values:
        raise ValidationError({"answer_type": _("Unknown answer type.")})
    if "config" in allowed or "answer_type" in allowed:
        raw = allowed.get("config", item.config if answer_type == item.answer_type else {})
        allowed["config"] = c.clean_config(answer_type, raw)
    changed_type = answer_type != item.answer_type
    changes = apply_changes(item, allowed)
    if changes:
        stamp_history(item, actor, VERBS["item_updated"])
        try:
            with transaction.atomic():
                item.save()
        except IntegrityError as exc:
            raise _db_error(exc) from exc
        if changed_type:
            _sync_fixed_options(item)
        if {"code", "answer_type", "config", "is_active"} & set(changes):  # rules may depend on these
            _assert_rules_valid(version, f"Changing {item.code}")
        _touch(template, actor)
        _log(verb=VERBS["item_updated"], organization=template.organization, actor=actor, target=item,
                     description=f"Updated item {item.code}", metadata={"changes": changes})
        # Configuration changes get their own, searchable events (a range or type change alters meaning).
        if "answer_type" in changes:
            _log(verb=VERBS["item_type_changed"], organization=template.organization, actor=actor, target=item,
                 description=f"Answer type of {item.code} changed to {item.answer_type}", metadata=changes["answer_type"])
        if "config" in changes:
            _log(verb=VERBS["item_config_changed"], organization=template.organization, actor=actor, target=item,
                 description=f"Answer configuration of {item.code} changed", metadata=changes["config"])
    return item


@transaction.atomic
def delete_item(item: ChecklistItem, *, actor: Any = None) -> None:
    template, version = _editable(item.section.version)
    item = ChecklistItem.objects.select_related("section").get(pk=item.pk)
    section, code = item.section, item.code
    stamp_history(item, actor, VERBS["item_removed"])
    item.delete()
    _renumber(section.items.order_by("sort_order"))
    _assert_rules_valid(version, f"Removing {code}")
    _touch(template, actor)
    _log(template=template, verb=VERBS["item_removed"], organization=template.organization, actor=actor,
                 description=f"Removed item {code} from {template.code}")


@transaction.atomic
def reorder_items(section: ChecklistSection, ordered_ids: Iterable[Any], *, actor: Any = None) -> None:
    template, _version = _editable(section.version)
    ordered = _apply_order(ChecklistItem.objects.select_for_update().filter(section=section), ordered_ids, "item")
    _renumber(ordered)
    _touch(template, actor)
    _log(verb=VERBS["items_reordered"], organization=template.organization, actor=actor, target=section,
                 description=f"Reordered items in “{section.name}”")


def move_item(item: ChecklistItem, direction: str, *, actor: Any = None) -> None:
    ids = list(item.section.items.order_by("sort_order").values_list("pk", flat=True))
    reorder_items(item.section, _moved(ids, item.pk, direction), actor=actor)


def _require_custom_options(item: ChecklistItem) -> None:
    if item.answer_type not in c.CUSTOM_OPTIONS:
        raise ValidationError(_("Options can only be edited on single-choice and multiple-choice items."))


@transaction.atomic
def create_option(item: ChecklistItem, *, label: str, value: str = "", actor: Any = None, log: bool = True) -> ChecklistOption:
    template, _version = _editable(item.section.version)
    item = ChecklistItem.objects.get(pk=item.pk)
    _require_custom_options(item)
    label = (label or "").strip()
    if not label:
        raise ValidationError({"label": _("An option needs a label.")})
    value = (value or slugify(label).replace("-", "_")).strip().lower()[:64]
    if not _VALUE_RE.match(value):
        raise ValidationError({"value": _("The value may use lowercase letters, digits, “_”, “-” and “.”.")})
    option = ChecklistOption(item=item, value=value, label=label, sort_order=_next_order(item.options))
    stamp_history(option, actor, VERBS["option_added"])
    try:
        with transaction.atomic():
            option.save()
    except IntegrityError as exc:
        raise _db_error(exc) from exc
    if _log:
        _touch(template, actor)
        _log(verb=VERBS["option_added"], organization=template.organization, actor=actor, target=option,
                     description=f"Added option “{label}” to {item.code}")
    return option


@transaction.atomic
def update_option(option: ChecklistOption, *, actor: Any = None, **data: Any) -> ChecklistOption:
    template, _version = _editable(option.item.section.version)
    option = ChecklistOption.objects.select_related("item").get(pk=option.pk)
    _require_custom_options(option.item)
    allowed = {k: v for k, v in data.items() if k in ("label", "is_active")}  # value is stable
    if "label" in allowed and not (allowed["label"] or "").strip():
        raise ValidationError({"label": _("An option needs a label.")})
    changes = apply_changes(option, allowed)
    if changes:
        stamp_history(option, actor, VERBS["option_updated"])
        option.save()
        if "is_active" in changes:
            _assert_rules_valid(_version, f"Changing an option of {option.item.code}")
        _touch(template, actor)
        _log(verb=VERBS["option_updated"], organization=template.organization, actor=actor, target=option,
                     description=f"Updated option on {option.item.code}", metadata={"changes": changes})
    return option


@transaction.atomic
def delete_option(option: ChecklistOption, *, actor: Any = None) -> None:
    template, _version = _editable(option.item.section.version)
    option = ChecklistOption.objects.select_related("item").get(pk=option.pk)
    _require_custom_options(option.item)
    item, label = option.item, option.label
    stamp_history(option, actor, VERBS["option_removed"])
    option.delete()
    _renumber(item.options.order_by("sort_order"))
    _assert_rules_valid(_version, f"Removing the option “{label}”")
    _touch(template, actor)
    _log(template=template, verb=VERBS["option_removed"], organization=template.organization, actor=actor,
                 description=f"Removed option “{label}” from {item.code}")


@transaction.atomic
def reorder_options(item: ChecklistItem, ordered_ids: Iterable[Any], *, actor: Any = None) -> None:
    template, _version = _editable(item.section.version)
    _require_custom_options(item)
    ordered = _apply_order(ChecklistOption.objects.select_for_update().filter(item=item), ordered_ids, "option")
    _renumber(ordered)
    _touch(template, actor)
    _log(verb=VERBS["options_reordered"], organization=template.organization, actor=actor, target=item,
                 description=f"Reordered options of {item.code}")


def parse_option_entries(options: Iterable[Any]) -> list[tuple[str, str]]:
    """Normalise option input (labels, ``(value, label)`` pairs or ``{"value", "label"}``) to ``(value, label)``."""
    wanted: list[tuple[str, str]] = []
    for entry in options or ():
        if isinstance(entry, dict):
            unknown = set(entry) - {"value", "label"}
            if unknown:
                raise ValidationError({"options": _("Unexpected option fields: %(f)s.") % {"f": ", ".join(sorted(unknown))}})
            value, label = entry.get("value", ""), entry.get("label", "")
        elif isinstance(entry, (tuple, list)) and len(entry) == 2:
            value, label = entry
        elif isinstance(entry, str):
            value, label = "", entry
        else:
            raise ValidationError({"options": _("Options must be labels or {value, label} objects.")})
        if not isinstance(label, str) or not isinstance(value or "", str):
            raise ValidationError({"options": _("Option values and labels must be text.")})
        label = label.strip()
        if not label or len(label) > 200:
            raise ValidationError({"options": _("Every option needs a label of at most 200 characters.")})
        value = (value or slugify(label).replace("-", "_")).strip().lower()[:64]
        if not _VALUE_RE.match(value):
            raise ValidationError({"options": _("Option values may use lowercase letters, digits, “_”, “-” and “.”.")})
        wanted.append((value, label))
    if len(wanted) > 100:
        raise ValidationError({"options": _("An item may have at most 100 options.")})
    return wanted


@transaction.atomic
def set_options(item: ChecklistItem, options: Iterable[Any], *, actor: Any = None) -> list[ChecklistOption]:
    """Replace the author-defined options of a choice item with ``options`` (the order given is the display order).

    Each entry is a label, a ``(value, label)`` pair or ``{"value", "label"}``.  Existing options are matched by
    ``value`` (their label is updated, their identity kept), new ones are created, missing ones removed.  One audited
    ``checklist_option.synced`` event records what changed.  The shared path for the API and imports.
    """
    template, _version = _editable(item.section.version)
    item = ChecklistItem.objects.get(pk=item.pk)
    _require_custom_options(item)
    wanted = parse_option_entries(options)
    values = [v for v, _l in wanted]
    if len(set(values)) != len(values):
        raise ValidationError({"options": _("Option values must be unique.")})
    existing = {o.value: o for o in item.options.all()}
    added = [v for v in values if v not in existing]
    removed = [v for v in existing if v not in values]
    for value in removed:
        stamp_history(existing[value], actor, VERBS["option_removed"])
        existing[value].delete()
    # Park survivors above the new range so the deferred unique ordering never collides mid-rewrite.
    result: list[ChecklistOption] = []
    for position, (value, label) in enumerate(wanted, start=1):
        option = existing.get(value)
        if option is None:
            option = ChecklistOption(item=item, value=value, label=label, sort_order=position)
            stamp_history(option, actor, VERBS["option_added"])
            option.save()
        elif option.label != label or option.sort_order != position:
            option.label, option.sort_order = label, position
            stamp_history(option, actor, VERBS["option_updated"])
            option.save()
        result.append(option)
    if removed:
        _assert_rules_valid(_version, f"Changing the options of {item.code}")
    if added or removed:
        _touch(template, actor)
        _log(verb=VERBS["options_synced"], organization=template.organization, actor=actor, target=item,
             description=f"Updated the options of {item.code}", metadata={"added": added, "removed": removed})
    return result


def move_option(option: ChecklistOption, direction: str, *, actor: Any = None) -> None:
    ids = list(option.item.options.order_by("sort_order").values_list("pk", flat=True))
    reorder_options(option.item, _moved(ids, option.pk, direction), actor=actor)


# ---------------------------------------------------------------------------
# Rules (Phase 6): declarative conditions and actions, validated as a whole set
# ---------------------------------------------------------------------------
def _rule_text(name: str, description: str) -> tuple[str, str]:
    name, description = (name or "").strip(), (description or "").strip()
    if not name:
        raise ValidationError({"name": _("A rule needs a name.")})
    if len(name) > 120:
        raise ValidationError({"name": _("A rule name may be at most 120 characters.")})
    if len(description) > 300:
        raise ValidationError({"description": _("A description may be at most 300 characters.")})
    return name, description


def _validated(version: ChecklistVersion, candidate: RuleSpec, replacing: str | None = None):  # noqa: ANN202
    """Validate the version's rule set with ``candidate`` added (or replacing ``replacing``); return its compiled form."""
    snapshot = snapshot_for_version(version)
    others = [r for r in snapshot.rules if r.code != replacing]
    report = validate_rule_set(snapshot.with_rules([*others, candidate]))
    if report.errors:
        raise RuleValidationError(report.errors)
    return next(r for r in report.compiled if r.code == candidate.code)


def create_rule(
    version: ChecklistVersion, *, name: str, actions: Any, condition: Any = None, description: str = "", code: str = "",
    is_active: bool = True, actor: Any = None,
) -> ChecklistRule:
    """Add a rule to a draft version.  The whole rule set is re-validated (references, operators, values, cycles, conflicts)."""
    try:
        return _create_rule(version, name=name, actions=actions, condition=condition, description=description, code=code,
                            is_active=is_active, actor=actor)
    except RuleValidationError as exc:
        template = ChecklistTemplate.objects.select_related("organization").get(pk=version.template_id)
        _log_rule_failure(template, version, actor, exc.issues, "creating a rule")
        raise


@transaction.atomic
def _create_rule(version, *, name, actions, condition, description, code, is_active, actor) -> ChecklistRule:  # noqa: ANN001
    template, version = _editable(version)
    name, description = _rule_text(name, description)
    existing = list(version.rules.values_list("code", flat=True))
    code = normalize_code(code) or _next_code("R", existing)
    validate_code(code)
    if code in existing:
        raise ValidationError({"code": _("This rule code is already used in this checklist version.")})
    order = _next_order(version.rules)
    compiled = _validated(version, RuleSpec(code, name, condition, actions, is_active, order, description))
    rule = ChecklistRule(
        version=version, code=code, name=name, description=description, condition=compiled.stored_condition,
        actions=compiled.stored_actions, sort_order=order, is_active=is_active,
    )
    stamp_history(rule, actor, VERBS["rule_created"])
    rule.save()
    _touch(template, actor)
    _log(verb=VERBS["rule_created"], organization=template.organization, actor=actor, target=rule,
         description=f"Created rule {code} “{name}” on {template.code}",
         metadata={"code": code, "enabled": is_active, "actions": [a["type"] for a in compiled.stored_actions]})
    return rule


def update_rule(rule: ChecklistRule, *, actor: Any = None, **data: Any) -> ChecklistRule:
    """Change ``name``, ``description``, ``condition`` and/or ``actions`` (the code is permanent)."""
    try:
        return _update_rule(rule, actor=actor, **data)
    except RuleValidationError as exc:
        version = ChecklistVersion.objects.select_related("template__organization").get(pk=rule.version_id)
        _log_rule_failure(version.template, version, actor, exc.issues, f"updating rule {rule.code}")
        raise


@transaction.atomic
def _update_rule(rule: ChecklistRule, *, actor: Any = None, **data: Any) -> ChecklistRule:
    template, version = _editable(rule.version)
    rule = ChecklistRule.objects.get(pk=rule.pk)
    allowed = {k: v for k, v in data.items() if k in ("name", "description", "condition", "actions")}
    name, description = _rule_text(allowed.get("name", rule.name), allowed.get("description", rule.description))
    candidate = RuleSpec(rule.code, name, allowed.get("condition", rule.condition), allowed.get("actions", rule.actions),
                         rule.is_active, rule.sort_order, description)
    compiled = _validated(version, candidate, replacing=rule.code)
    changes = apply_changes(rule, {"name": name, "description": description, "condition": compiled.stored_condition,
                                   "actions": compiled.stored_actions})
    if changes:
        stamp_history(rule, actor, VERBS["rule_updated"])
        rule.save()
        _touch(template, actor)
        _log(verb=VERBS["rule_updated"], organization=template.organization, actor=actor, target=rule,
             description=f"Updated rule {rule.code}", metadata={"changes": sorted(changes)})
        if "condition" in changes:
            _log(verb=VERBS["rule_condition_changed"], organization=template.organization, actor=actor, target=rule,
                 description=f"Condition of rule {rule.code} changed", metadata=changes["condition"])
        if "actions" in changes:
            _log(verb=VERBS["rule_actions_changed"], organization=template.organization, actor=actor, target=rule,
                 description=f"Actions of rule {rule.code} changed", metadata=changes["actions"])
    return rule


def set_rule_enabled(rule: ChecklistRule, enabled: bool, *, actor: Any = None) -> ChecklistRule:
    """Enable or disable a rule.  Enabling re-validates the set (a disabled rule may hide a cycle)."""
    try:
        return _set_rule_enabled(rule, enabled, actor=actor)
    except RuleValidationError as exc:
        version = ChecklistVersion.objects.select_related("template__organization").get(pk=rule.version_id)
        _log_rule_failure(version.template, version, actor, exc.issues, f"enabling rule {rule.code}")
        raise


@transaction.atomic
def _set_rule_enabled(rule: ChecklistRule, enabled: bool, *, actor: Any = None) -> ChecklistRule:
    template, version = _editable(rule.version)
    rule = ChecklistRule.objects.get(pk=rule.pk)
    if rule.is_active == enabled:
        return rule
    if enabled:
        _validated(version, RuleSpec(rule.code, rule.name, rule.condition, rule.actions, True, rule.sort_order, rule.description),
                   replacing=rule.code)
    rule.is_active = enabled
    verb = VERBS["rule_enabled" if enabled else "rule_disabled"]
    stamp_history(rule, actor, verb)
    rule.save()
    _touch(template, actor)
    _log(verb=verb, organization=template.organization, actor=actor, target=rule,
         description=f"{'Enabled' if enabled else 'Disabled'} rule {rule.code}")
    return rule


@transaction.atomic
def delete_rule(rule: ChecklistRule, *, actor: Any = None) -> None:
    template, version = _editable(rule.version)
    rule = ChecklistRule.objects.get(pk=rule.pk)
    code = rule.code
    stamp_history(rule, actor, VERBS["rule_removed"])
    rule.delete()
    _renumber(version.rules.order_by("sort_order"))
    _touch(template, actor)
    _log(template=template, verb=VERBS["rule_removed"], organization=template.organization, actor=actor,
         description=f"Deleted rule {code}", metadata={"code": code})


@transaction.atomic
def reorder_rules(version: ChecklistVersion, ordered_ids: Iterable[Any], *, actor: Any = None) -> None:
    """Set the evaluation/display order (it only decides message order and trace order, never the outcome)."""
    template, version = _editable(version)
    ordered = _apply_order(version.rules.select_for_update(), ordered_ids, "rule")
    _renumber(ordered)
    _touch(template, actor)
    _log(verb=VERBS["rules_reordered"], organization=template.organization, actor=actor, target=version,
         description=f"Reordered the rules of {template.code}")


def move_rule(rule: ChecklistRule, direction: str, *, actor: Any = None) -> None:
    ids = list(rule.version.rules.order_by("sort_order").values_list("pk", flat=True))
    reorder_rules(rule.version, _moved(ids, rule.pk, direction), actor=actor)
