"""The validated, ready-to-evaluate form of a rule (what ``schema`` produces and the engine consumes)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .operators import OperatorDef


@dataclass(frozen=True)
class Condition:
    item_code: str
    operator: OperatorDef
    family: str
    operand: Any  # parsed Python value the evaluator compares
    stored: Any  # JSON-safe normalized value (as saved)


@dataclass(frozen=True)
class Group:
    logic: str  # "AND" | "OR"
    children: tuple[Condition | Group, ...]


@dataclass(frozen=True)
class CompiledAction:
    type: str
    target_kind: str  # "item" | "section" | "" (checklist-level message)
    target_code: str
    params: dict[str, Any]  # normalized parameters (JSON-safe)

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"type": self.type, **self.params}
        if self.target_kind:
            out["target"] = {"type": self.target_kind, "code": self.target_code}
        return out


@dataclass(frozen=True)
class CompiledRule:
    code: str
    name: str
    description: str
    is_active: bool
    sort_order: int
    condition: Group | None  # None = unconditional
    actions: tuple[CompiledAction, ...]
    #: Normalized JSON written back to the database (condition/actions).
    stored_condition: dict[str, Any] | None
    stored_actions: list[dict[str, Any]]

    def sources(self) -> frozenset[str]:
        found: set[str] = set()

        def walk(node: Condition | Group | None) -> None:
            if isinstance(node, Condition):
                found.add(node.item_code)
            elif isinstance(node, Group):
                for child in node.children:
                    walk(child)

        walk(self.condition)
        return frozenset(found)
