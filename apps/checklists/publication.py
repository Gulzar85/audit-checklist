"""Pre-publication validation: one report, used by the builder, the API, the publish service and imports alike.

``validate_for_publication(version)`` inspects a version *as it is stored* and returns a :class:`PublicationReport` with
**errors** (block publication) and **warnings** (informational; never blocking unless a documented rule says so).  It reuses
the Phase 5 answer-type engine for every configuration and the Phase 6 rule validator for every rule - there is no second
copy of those rules here.  It is a pure read: it changes nothing and logs nothing (the explicit "Validate" action in
``lifecycle`` records that a validation was run).

What is checked (``code`` of each finding in parentheses)

* template - active organization, not archived, named (``organization_inactive``, ``template_archived``, ``template_name_missing``);
* version - is a draft of this template, positive number, a valid base version (``not_draft``, ``bad_version_number``,
  ``invalid_base``), has at least one section (``no_sections``);
* sections - named, valid and unique codes, contiguous order, at least one active question
  (``section_name_missing``, ``invalid_section_code``, ``duplicate_section_code``, ``bad_section_order``, ``empty_section``);
* items - question text, a registered answer type, a configuration the answer-type engine accepts, valid reference
  data, valid and *version-wide unique* codes, contiguous order
  (``question_missing``, ``unknown_answer_type``, ``invalid_config``, ``invalid_metadata``, ``invalid_item_code``,
  ``duplicate_item_code``, ``bad_item_order``);
* options - choice types have at least two active options, fixed-option types carry exactly the platform's options,
  other types carry none, option values are valid, labels present, order contiguous
  (``too_few_options``, ``fixed_options_mismatch``, ``unexpected_options``, ``invalid_option_value``, ``option_label_missing``,
  ``bad_option_order``);
* rules - everything ``rules.validate_rule_set`` checks (references are resolved *inside this version only*, operators suit
  the answer types, values parse, no cycles, no definite conflicts), reported with the rule's own issue codes.

Warnings: ``no_instructions``, ``all_optional``, ``inactive_items``, ``no_change_summary``, ``no_changes`` (identical to the
published version) and Phase 6's ``possible_conflict`` / ``inactive_source`` / ``inactive_target``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from apps.locations.models import validate_code
from django.core.exceptions import ValidationError

from . import constants as c
from .answer_types import ConfigurationError, registry
from .manifest import hash_manifest, manifest_from_rows
from .metadata import clean_metadata
from .rules import snapshot_for_version, validate_rule_set
from . import selectors

_VALUE_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,63}$")


@dataclass(frozen=True)
class Finding:
    level: str  # "error" | "warning"
    code: str
    message: str
    subject: str = ""  # "version", "template", "section:S-001", "item:Q-001", "rule:R-001"

    def as_dict(self) -> dict[str, str]:
        out = {"level": self.level, "code": self.code, "message": self.message}
        if self.subject:
            out["subject"] = self.subject
        return out


@dataclass
class PublicationReport:
    version_id: str
    version_number: int
    template_code: str
    findings: list[Finding] = field(default_factory=list)
    stats: dict[str, int] = field(default_factory=dict)
    #: Hash of the draft's current definition: the token a publish request must present (``expected_hash``).
    draft_hash: str = ""

    @property
    def errors(self) -> list[Finding]:
        return [f for f in self.findings if f.level == "error"]

    @property
    def warnings(self) -> list[Finding]:
        return [f for f in self.findings if f.level == "warning"]

    @property
    def ok(self) -> bool:
        return not self.errors

    def messages(self) -> list[str]:
        """The blocking problems as plain strings (the pre-Phase-7 ``validate_version_complete`` contract)."""
        return [f.message for f in self.errors]

    def as_dict(self) -> dict[str, Any]:
        return {
            "valid": self.ok, "version": self.version_number, "draft_hash": self.draft_hash, "stats": self.stats,
            "errors": [f.as_dict() for f in self.errors], "warnings": [f.as_dict() for f in self.warnings],
        }


def _contiguous(orders: list[int]) -> bool:
    return orders == list(range(1, len(orders) + 1))


def validate_for_publication(version: Any) -> PublicationReport:
    """Validate ``version`` for publication.  Never raises for bad *data*; never writes."""
    template = version.template
    organization = template.organization
    sections = list(selectors.get_sections_for_version(version))
    rules = list(version.rules.order_by("sort_order"))
    report = PublicationReport(str(version.pk), version.version_number, template.code)
    add = report.findings.append

    def error(code: str, message: str, subject: str = "") -> None:
        add(Finding("error", code, message, subject))

    def warn(code: str, message: str, subject: str = "") -> None:
        add(Finding("warning", code, message, subject))

    # -- template ------------------------------------------------------------------------------------------
    if not organization.is_active:
        error("organization_inactive", "This organization is not active, so its checklists cannot be published.", "template")
    if template.status == c.TemplateStatus.ARCHIVED:
        error("template_archived", "The checklist is archived and cannot be published. Restore it first.", "template")
    if not (template.name or "").strip():
        error("template_name_missing", "The checklist needs a name.", "template")
    if not (template.instructions or "").strip():
        warn("no_instructions", "No checklist instructions have been provided.", "template")

    # -- version -------------------------------------------------------------------------------------------
    if version.status != c.VersionStatus.DRAFT:
        error("not_draft", f"Version {version.version_number} is not a draft and cannot be published.", "version")
    if version.version_number < 1:
        error("bad_version_number", "The version number is invalid.", "version")
    if version.based_on_id and version.based_on.template_id != version.template_id:
        error("invalid_base", "The version is based on a version of a different checklist.", "version")
    if not sections:
        error("no_sections", "Add at least one section.", "version")

    # -- sections, items, options ---------------------------------------------------------------------------
    seen_sections: set[str] = set()
    seen_items: dict[str, str] = {}
    item_total = option_total = required_total = inactive_total = 0
    if not _contiguous([s.sort_order for s in sections]):
        error("bad_section_order", "The section order is not a clean 1..n sequence; reorder the sections.", "version")
    for section in sections:
        subject = f"section:{section.code}"
        if not (section.name or "").strip():
            error("section_name_missing", f"Section {section.code} has no name.", subject)
        try:
            validate_code(section.code)
        except ValidationError:
            error("invalid_section_code", f"Section code “{section.code}” is not valid.", subject)
        if section.code in seen_sections:
            error("duplicate_section_code", f"Section code “{section.code}” is used more than once.", subject)
        seen_sections.add(section.code)
        items = list(section.items.all())
        active = [i for i in items if i.is_active]
        if not active:
            error("empty_section", f"Section “{section.name}” has no active items.", subject)
        if not _contiguous([i.sort_order for i in items]):
            error("bad_item_order", f"The question order in section “{section.name}” is not a clean 1..n sequence.", subject)
        for item in items:
            item_total += 1
            inactive_total += 0 if item.is_active else 1
            required_total += 1 if item.is_required and item.is_active else 0
            subject = f"item:{item.code}"
            if not (item.question or "").strip():
                error("question_missing", f"Item {item.code} has no question text.", subject)
            try:
                validate_code(item.code)
            except ValidationError:
                error("invalid_item_code", f"Item code “{item.code}” is not valid.", subject)
            if item.code in seen_items:
                error("duplicate_item_code",
                      f"Item code “{item.code}” is used more than once in this version (sections {seen_items[item.code]} and {section.code}).",
                      subject)
            seen_items.setdefault(item.code, section.code)
            if item.answer_type not in registry:
                error("unknown_answer_type", f"Item {item.code} has an unknown answer type “{item.answer_type}”.", subject)
            else:
                try:
                    registry.clean_config(item.answer_type, item.config)
                except ConfigurationError as exc:
                    error("invalid_config", f"Item {item.code} has an invalid configuration: {exc.message}", subject)
            try:
                clean_metadata(item.metadata)
            except ValidationError as exc:
                error("invalid_metadata", f"Item {item.code} has invalid reference data: {' '.join(exc.messages)}", subject)
            options = list(item.options.all())
            option_total += len(options)
            _check_options(item, options, error, subject)

    # -- rules ---------------------------------------------------------------------------------------------
    if rules:
        rule_report = validate_rule_set(snapshot_for_version(version))
        for issue in rule_report.errors:
            error(issue.code, f"Rule {issue.rule}: {issue.message}" if issue.rule else issue.message, f"rule:{issue.rule}" if issue.rule else "version")
        for issue in rule_report.warnings:
            warn(issue.code, issue.message, f"rule:{issue.rule}" if issue.rule else "version")

    # -- warnings that need context --------------------------------------------------------------------------
    if item_total and not required_total:
        warn("all_optional", "This checklist contains only optional items.", "version")
    if inactive_total:
        warn("inactive_items", f"{inactive_total} inactive item{'s are' if inactive_total != 1 else ' is'} included (kept for history, never asked).", "version")
    manifest = manifest_from_rows(sections, rules)
    report.draft_hash = hash_manifest(manifest)
    current = version.template.versions.filter(status=c.VersionStatus.PUBLISHED).order_by("-version_number").first()
    if current is not None and version.status == c.VersionStatus.DRAFT:
        if current.integrity_hash and current.integrity_hash == report.draft_hash:
            warn("no_changes", f"This draft is identical to published version {current.version_number}; publishing it adds nothing.", "version")
        if not (version.change_summary or "").strip():
            warn("no_change_summary", f"No change summary describes what changed since version {current.version_number}.", "version")
    report.stats = {"sections": len(sections), "items": item_total, "options": option_total, "rules": len(rules)}
    return report


def _check_options(item: Any, options: list[Any], error: Any, subject: str) -> None:
    if item.answer_type in c.CUSTOM_OPTIONS:
        if sum(1 for o in options if o.is_active) < 2:
            error("too_few_options", f"Item {item.code} needs at least two options.", subject)
    elif item.answer_type in c.FIXED_OPTIONS:
        wanted = [value for value, _label in c.FIXED_OPTIONS[item.answer_type]]
        if [o.value for o in options] != wanted:
            error("fixed_options_mismatch", f"Item {item.code} must carry exactly the platform options ({', '.join(wanted)}).", subject)
    elif options:
        error("unexpected_options", f"Item {item.code} ({item.answer_type}) must not have options.", subject)
    for option in options:
        if not _VALUE_RE.match(option.value or ""):
            error("invalid_option_value", f"Option value “{option.value}” of item {item.code} is not valid.", subject)
        if not (option.label or "").strip():
            error("option_label_missing", f"An option of item {item.code} has no label.", subject)
    if not _contiguous([o.sort_order for o in options]):
        error("bad_option_order", f"The option order of item {item.code} is not a clean 1..n sequence.", subject)
