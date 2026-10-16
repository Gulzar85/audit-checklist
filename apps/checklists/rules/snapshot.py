"""An immutable, database-free picture of one checklist version - everything the rules engine needs.

The evaluator and validators take a :class:`VersionSnapshot` and explicit answers; they never touch the database or
global state, so the same inputs always give the same result and the same code can later run for the PWA and offline
sync.  :func:`snapshot_for_version` builds one with a fixed number of queries.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..answer_types import AnswerContext, OptionRef, registry


@dataclass(frozen=True)
class OptionSpec:
    id: str
    value: str
    label: str
    sort_order: int
    is_active: bool


@dataclass(frozen=True)
class ItemSpec:
    id: str
    code: str
    section_code: str
    question: str
    answer_type: str
    config: dict[str, Any]  # normalized (defaults filled)
    required: bool
    active: bool
    sort_order: int
    options: tuple[OptionSpec, ...] = ()

    @property
    def definition(self):  # noqa: ANN201
        return registry.get(self.answer_type)

    def option_values(self) -> list[str]:
        return [o.value for o in self.options]

    def context(self, *, config: dict[str, Any] | None = None, options: tuple[OptionSpec, ...] | None = None,
                required: bool = False) -> AnswerContext:
        chosen = self.options if options is None else options
        return AnswerContext(
            answer_type=self.answer_type,
            config=self.config if config is None else config,
            options=tuple(OptionRef(id=o.id, value=o.value, label=o.label, sort_order=o.sort_order) for o in chosen if o.is_active),
            required=required, item_id=self.code,
        )


@dataclass(frozen=True)
class SectionSpec:
    id: str
    code: str
    name: str
    sort_order: int


@dataclass(frozen=True)
class RuleSpec:
    """A rule as stored (raw JSON); it is validated/compiled by ``schema`` before being trusted."""

    code: str
    name: str
    condition: Any
    actions: Any
    is_active: bool = True
    sort_order: int = 0
    description: str = ""
    id: str = ""


@dataclass(frozen=True)
class VersionSnapshot:
    version_id: str
    organization_id: str
    sections: dict[str, SectionSpec] = field(default_factory=dict)
    items: dict[str, ItemSpec] = field(default_factory=dict)
    rules: tuple[RuleSpec, ...] = ()

    def items_in_section(self, section_code: str) -> list[ItemSpec]:
        return [i for i in self.items.values() if i.section_code == section_code]

    def with_rules(self, rules: tuple[RuleSpec, ...] | list[RuleSpec]) -> VersionSnapshot:
        """The same version with a different (e.g. candidate) rule set."""
        return VersionSnapshot(self.version_id, self.organization_id, self.sections, self.items, tuple(rules))


def snapshot_for_version(version: Any, *, include_rules: bool = True) -> VersionSnapshot:
    """Load ``version`` (sections, items, options, rules) in a constant number of queries."""
    from ..selectors import get_sections_for_version

    sections: dict[str, SectionSpec] = {}
    items: dict[str, ItemSpec] = {}
    for section in get_sections_for_version(version):
        sections[section.code] = SectionSpec(str(section.pk), section.code, section.name, section.sort_order)
        for item in section.items.all():
            definition = registry.get(item.answer_type) if item.answer_type in registry else None
            items[item.code] = ItemSpec(
                id=str(item.pk), code=item.code, section_code=section.code, question=item.question,
                answer_type=item.answer_type,
                config=definition.config_with_defaults(item.config) if definition else {},
                required=item.is_required, active=item.is_active, sort_order=item.sort_order,
                options=tuple(OptionSpec(str(o.pk), o.value, o.label, o.sort_order, o.is_active) for o in item.options.all()),
            )
    rules: tuple[RuleSpec, ...] = ()
    if include_rules:
        rules = tuple(
            RuleSpec(code=r.code, name=r.name, condition=r.condition, actions=r.actions, is_active=r.is_active,
                     sort_order=r.sort_order, description=r.description, id=str(r.pk))
            for r in version.rules.order_by("sort_order", "code")
        )
    return VersionSnapshot(
        version_id=str(version.pk), organization_id=str(version.template.organization_id),
        sections=sections, items=items, rules=rules,
    )
