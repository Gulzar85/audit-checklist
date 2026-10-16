"""Business operations for checklist definitions.

Every write - from the builder UI, the API, imports and the admin actions - goes through here, so
validation, locking, version safety, history and the activity log apply uniformly.  Callers authorize
first (``apps.checklists.policies``); services enforce the *data* rules that must hold for every writer:

* a **published version is immutable** - structure changes are refused, create a new draft version instead;
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
    "section_added": "checklist_section.added",
    "section_updated": "checklist_section.updated",
    "section_removed": "checklist_section.removed",
    "sections_reordered": "checklist_section.reordered",
    "item_added": "checklist_item.added",
    "item_updated": "checklist_item.updated",
    "item_removed": "checklist_item.removed",
    "items_reordered": "checklist_item.reordered",
    "option_added": "checklist_option.added",
    "option_updated": "checklist_option.updated",
    "option_removed": "checklist_option.removed",
    "options_reordered": "checklist_option.reordered",
    "rule_added": "checklist_rule.added",
    "rule_removed": "checklist_rule.removed",
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
            _("Version %(n)s is published and cannot be changed. Create a new draft version to make changes.")
            % {"n": fresh.version_number},
            code="version_immutable",
        )
    if template.status == c.TemplateStatus.ARCHIVED:
        raise ValidationError(_("An archived checklist cannot be changed. Restore it first."), code="template_archived")
    return template, fresh


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
    if template.status != c.TemplateStatus.DRAFT or template.versions.filter(status=c.VersionStatus.PUBLISHED).exists():
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
    """Copy sections, items, options and rules of ``source`` into ``target`` with new identifiers."""
    item_map: dict[Any, ChecklistItem] = {}
    section_map: dict[Any, ChecklistSection] = {}
    for section in source.sections.prefetch_related("items__options").order_by("sort_order"):
        new_section = ChecklistSection.objects.create(
            version=target, name=section.name, description=section.description, instructions=section.instructions,
            sort_order=section.sort_order, is_required=section.is_required,
        )
        section_map[section.pk] = new_section
        for item in section.items.all():
            new_item = ChecklistItem.objects.create(
                section=new_section, code=item.code, question=item.question, description=item.description,
                help_text=item.help_text, instructions=item.instructions, answer_type=item.answer_type,
                config=item.config, is_required=item.is_required, is_active=item.is_active, sort_order=item.sort_order,
            )
            item_map[item.pk] = new_item
            ChecklistOption.objects.bulk_create(
                [ChecklistOption(item=new_item, value=o.value, label=o.label, sort_order=o.sort_order, is_active=o.is_active)
                 for o in item.options.all()]
            )
    for rule in source.rules.all():
        ChecklistRule.objects.create(
            version=target, source_item=item_map[rule.source_item_id], operator=rule.operator, value=rule.value,
            action=rule.action, sort_order=rule.sort_order, is_active=rule.is_active,
            target_item=item_map.get(rule.target_item_id), target_section=section_map.get(rule.target_section_id),
        )


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
@transaction.atomic
def create_checklist_version(template: ChecklistTemplate, *, actor: Any = None, change_summary: str = "") -> ChecklistVersion:
    """Start the next draft version as a copy of the latest one (refused while a draft exists)."""
    template = _lock_template(template.pk)
    if template.status == c.TemplateStatus.ARCHIVED:
        raise ValidationError(_("An archived checklist cannot be changed. Restore it first."))
    if template.versions.filter(status=c.VersionStatus.DRAFT).exists():
        raise ValidationError(_("This checklist already has a draft version. Publish or finish it first."))
    latest = template.versions.order_by("-version_number").first()
    version = _new_draft_version(template, actor, change_summary.strip()[:300])
    if latest is not None:
        _clone_structure(latest, version, actor)
    _touch(template, actor)
    _log(verb=VERBS["version_created"], organization=template.organization, actor=actor, target=version,
                 description=f"Created draft version {version.version_number} of {template.code}")
    return version


def validate_version_complete(version: ChecklistVersion) -> list[str]:
    """Problems that stop a version being published (empty list = publishable)."""
    problems: list[str] = []
    sections = list(version.sections.prefetch_related("items__options"))
    if not sections:
        problems.append(str(_("Add at least one section.")))
    for section in sections:
        active = [i for i in section.items.all() if i.is_active]
        if not active:
            problems.append(str(_("Section “%(name)s” has no active items.") % {"name": section.name}))
        for item in active:
            if item.answer_type in c.CUSTOM_OPTIONS and sum(1 for o in item.options.all() if o.is_active) < 2:
                problems.append(str(_("Item %(code)s needs at least two options.") % {"code": item.code}))
    return problems


@transaction.atomic
def publish_checklist_version(version: ChecklistVersion, *, actor: Any = None, change_summary: str | None = None) -> ChecklistVersion:
    """Freeze a draft.  From here the version is immutable (Phase 7 adds the full release workflow)."""
    template, version = _editable(version)
    problems = validate_version_complete(version)
    if problems:
        raise ValidationError(problems)
    if change_summary is not None:
        version.change_summary = change_summary.strip()[:300]
    version.status = c.VersionStatus.PUBLISHED
    version.published_at = timezone.now()
    stamp_history(version, actor, VERBS["version_published"])
    version.save()
    _touch(template, actor)
    _log(verb=VERBS["version_published"], organization=template.organization, actor=actor, target=version,
                 description=f"Published {template.code} v{version.version_number}")
    return version


# ---------------------------------------------------------------------------
# Sections
# ---------------------------------------------------------------------------
@transaction.atomic
def create_section(version: ChecklistVersion, *, name: str, actor: Any = None, description: str = "",
                   instructions: str = "", is_required: bool = True) -> ChecklistSection:
    template, version = _editable(version)
    name = (name or "").strip()
    if not name:
        raise ValidationError({"name": _("A section needs a name.")})
    section = ChecklistSection(version=version, name=name, description=description, instructions=instructions,
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
    )
    stamp_history(item, actor, VERBS["item_added"])
    try:
        with transaction.atomic():
            item.save()
    except IntegrityError as exc:
        raise _db_error(exc) from exc
    _sync_fixed_options(item)
    if answer_type in c.CUSTOM_OPTIONS:
        for value, label in options:
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
        "question", "code", "answer_type", "config", "description", "help_text", "instructions", "is_required", "is_active")}
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
        _touch(template, actor)
        _log(verb=VERBS["item_updated"], organization=template.organization, actor=actor, target=item,
                     description=f"Updated item {item.code}", metadata={"changes": changes})
    return item


@transaction.atomic
def delete_item(item: ChecklistItem, *, actor: Any = None) -> None:
    template, version = _editable(item.section.version)
    item = ChecklistItem.objects.select_related("section").get(pk=item.pk)
    section, code = item.section, item.code
    stamp_history(item, actor, VERBS["item_removed"])
    item.delete()
    _renumber(section.items.order_by("sort_order"))
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


def move_option(option: ChecklistOption, direction: str, *, actor: Any = None) -> None:
    ids = list(option.item.options.order_by("sort_order").values_list("pk", flat=True))
    reorder_options(option.item, _moved(ids, option.pk, direction), actor=actor)


# ---------------------------------------------------------------------------
# Rules (definition only; evaluated in Phase 6)
# ---------------------------------------------------------------------------
@transaction.atomic
def create_rule(
    version: ChecklistVersion, *, source_item: ChecklistItem, operator: str, action: str, actor: Any = None,
    value: Any = None, target_item: ChecklistItem | None = None, target_section: ChecklistSection | None = None,
) -> ChecklistRule:
    template, version = _editable(version)
    if operator not in c.RuleOperator.values or action not in c.RuleAction.values:
        raise ValidationError(_("Unknown rule operator or action."))
    if source_item.section.version_id != version.pk:
        raise ValidationError({"source_item": _("The source item belongs to another version.")})
    needs = c.TARGET_ACTIONS.get(action)
    if needs == "item" and (target_item is None or target_item.section.version_id != version.pk):
        raise ValidationError({"target_item": _("Choose a target item in this version.")})
    if needs == "section" and (target_section is None or target_section.version_id != version.pk):
        raise ValidationError({"target_section": _("Choose a target section in this version.")})
    if needs is None and (target_item or target_section):
        raise ValidationError(_("This action takes no target."))
    if operator != c.RuleOperator.ANSWERED and value is None:
        raise ValidationError({"value": _("This condition needs a value.")})
    rule = ChecklistRule(
        version=version, source_item=source_item, operator=operator, action=action, value=value,
        target_item=target_item if needs == "item" else None,
        target_section=target_section if needs == "section" else None,
        sort_order=_next_order(version.rules),
    )
    stamp_history(rule, actor, VERBS["rule_added"])
    rule.save()
    _touch(template, actor)
    _log(verb=VERBS["rule_added"], organization=template.organization, actor=actor, target=rule,
                 description=f"Added a rule on {source_item.code}")
    return rule


@transaction.atomic
def delete_rule(rule: ChecklistRule, *, actor: Any = None) -> None:
    template, _version = _editable(rule.version)
    stamp_history(rule, actor, VERBS["rule_removed"])
    rule.delete()
    _touch(template, actor)
    _log(template=template, verb=VERBS["rule_removed"], organization=template.organization, actor=actor,
                 description="Removed a rule")
