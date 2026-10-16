"""The answer-type registry: the one place that knows which answer types exist and what they are.

Views, forms, serializers, validators, templates and (later) the execution engine ask the registry; none of them
branches on a type code.  Adding a type is one definition class plus one registration (see ``docs/answer-types.md``).
"""

from __future__ import annotations

from typing import Any

from .base import AnswerTypeDefinition
from .definitions import ALL_DEFINITIONS
from .exceptions import ConfigurationError

#: Builder grouping, in display order.
CATEGORIES: tuple[tuple[str, str], ...] = (
    ("choice", "Choice"),
    ("value", "Value"),
    ("datetime", "Date & Time"),
    ("media", "Media"),
    ("advanced", "Advanced"),
)


class AnswerTypeRegistry:
    def __init__(self) -> None:
        self._definitions: dict[str, AnswerTypeDefinition] = {}

    def register(self, definition_class: type[AnswerTypeDefinition]) -> type[AnswerTypeDefinition]:
        definition = definition_class()
        if not definition.code or definition.code in self._definitions:
            raise ValueError(f"Answer type code {definition.code!r} is empty or already registered.")
        if definition.category not in dict(CATEGORIES):
            raise ValueError(f"Answer type {definition.code!r} has an unknown category {definition.category!r}.")
        self._definitions[definition.code] = definition
        return definition_class

    def get(self, code: str) -> AnswerTypeDefinition:
        try:
            return self._definitions[code]
        except KeyError:
            raise KeyError(f"Unknown answer type {code!r}.") from None

    def __contains__(self, code: object) -> bool:
        return code in self._definitions

    def all(self) -> list[AnswerTypeDefinition]:
        return list(self._definitions.values())

    def codes(self) -> list[str]:
        return list(self._definitions)

    def grouped(self) -> list[tuple[str, str, list[AnswerTypeDefinition]]]:
        """``[(category, label, [definitions])]`` in display order (for the builder's selector)."""
        return [
            (key, label, [d for d in self._definitions.values() if d.category == key]) for key, label in CATEGORIES
        ]

    def choices(self) -> list[tuple[str, list[tuple[str, str]]]]:
        """Grouped Django ``choices``: ``[(group label, [(code, label), ...])]``."""
        return [(label, [(d.code, d.label) for d in defs]) for _, label, defs in self.grouped() if defs]

    def describe(self, code: str | None = None, *, detail: bool = False) -> Any:
        if code is not None:
            return self.get(code).describe(detail=detail)
        return [d.describe(detail=detail) for d in self._definitions.values()]

    def clean_config(self, code: str, raw: Any) -> dict[str, Any]:
        """Validated, normalized, storable configuration for ``code`` (raises ``ConfigurationError``)."""
        if code not in self:
            raise ConfigurationError(f"Unknown answer type “{code}”.")
        return self.get(code).clean_config(raw)


registry = AnswerTypeRegistry()
for _definition in ALL_DEFINITIONS:
    registry.register(_definition)


def get_answer_type(code: str) -> AnswerTypeDefinition:
    return registry.get(code)


def get_answer_type_definitions() -> list[AnswerTypeDefinition]:
    return registry.all()
