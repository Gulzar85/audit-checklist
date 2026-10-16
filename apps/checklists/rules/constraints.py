"""Rule-driven validation constraints (``SET_VALIDATION_CONSTRAINT``): tighten only, through the Answer Type Engine.

A rule can *narrow* what an item accepts - for instance "if the area is a freezer, the temperature must be at most -15" -
but it can never widen the item's own limits and never invent a setting the answer type does not declare: the allowed
keys are the type's ``rule_constraints`` and each value is parsed with the type's own configuration schema.  The merged
configuration must still pass the type's ``clean_config``, so a rule cannot produce a configuration the engine would not
accept on its own.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any

from ..answer_types import ConfigurationError
from ..answer_types.schemas import ConfigField, clean_setting, jsonable
from .snapshot import ItemSpec


class ConstraintError(Exception):
    pass


def _spec(item_or_definition: Any, key: str) -> ConfigField:
    definition = getattr(item_or_definition, "definition", item_or_definition)
    return next(f for f in definition.config_fields if f.key == key)


def _comparable(spec: ConfigField, value: Any) -> Any:
    if spec.kind in ("int", "number"):
        return Decimal(str(value))
    if spec.kind == "date":
        return date.fromisoformat(value) if isinstance(value, str) else value
    if spec.kind == "datetime":
        return datetime.fromisoformat(value) if isinstance(value, str) else value
    return value


def clean_constraints(item: ItemSpec, raw: Any) -> dict[str, Any]:
    """Validate a rule's constraint object for ``item``; returns the JSON-safe normalized constraints."""
    definition = item.definition
    allowed = definition.rule_constraints
    if not allowed:
        raise ConstraintError(f"{definition.label} questions do not accept validation constraints from rules.")
    if not isinstance(raw, dict) or not raw:
        raise ConstraintError("Give at least one constraint, e.g. {\"max\": 8}.")
    unknown = sorted(set(raw) - set(allowed))
    if unknown:
        raise ConstraintError(f"Not available for {definition.label}: {', '.join(unknown)}. Allowed: {', '.join(allowed)}.")
    cleaned: dict[str, Any] = {}
    for key, value in raw.items():
        spec = _spec(item, key)
        try:
            cleaned[key] = clean_setting(spec, value)
        except ConfigurationError as exc:
            raise ConstraintError(f"“{key}” {exc.message}") from None
        base = item.config.get(key)
        if base is not None:
            new, old = _comparable(spec, cleaned[key]), _comparable(spec, base)
            if (key.startswith("min") and new < old) or (key.startswith("max") and new > old):
                raise ConstraintError(f"“{key}” cannot loosen the question's own limit ({base}); a rule may only tighten it.")
    try:
        definition.clean_config({**item.config, **jsonable(cleaned)})
    except ConfigurationError as exc:
        raise ConstraintError(exc.message) from None
    return jsonable(cleaned)


def tighten(item: ItemSpec, constraint_sets: list[dict[str, Any]]) -> tuple[dict[str, Any], str | None]:
    """The item's configuration with every matched constraint applied (the tightest value of each key wins).

    Returns ``(config, conflict)``; on a conflict (the combination is not a valid configuration, e.g. min above max)
    the *base* configuration is returned together with a message - contradictory rules never half-apply.
    """
    merged: dict[str, Any] = {}
    for constraints in constraint_sets:
        for key, value in constraints.items():
            spec = _spec(item, key)
            if key not in merged:
                merged[key] = value
                continue
            old, new = _comparable(spec, merged[key]), _comparable(spec, value)
            if (key.startswith("min") and new > old) or (key.startswith("max") and new < old):
                merged[key] = value
    try:
        return item.definition.clean_config({**item.config, **merged}), None
    except ConfigurationError as exc:
        return item.config, f"The rules' constraints on {item.code} cannot all be satisfied ({exc.message})"
