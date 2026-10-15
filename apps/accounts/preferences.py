"""The allow-list of user preferences (ADR 0037).

Each preference has a validator; unknown keys are rejected.  Only what a user may
legitimately choose for themselves is here: a time zone and a language that override the
organization's defaults.  Appearance lives in the theme engine, not here.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from django.core.exceptions import ValidationError

from apps.core.validators import validate_language_code, validate_timezone


@dataclass(frozen=True)
class PreferenceSpec:
    key: str
    label: str
    validator: Callable[[str], None]
    max_length: int


PREFERENCES: dict[str, PreferenceSpec] = {
    spec.key: spec
    for spec in (
        PreferenceSpec("timezone", "Time zone", validate_timezone, 64),
        PreferenceSpec("language", "Language", validate_language_code, 10),
    )
}


def validate_preference(key: str, value: Any) -> str:
    """Return the canonical value for ``key`` or raise ``ValidationError``."""
    spec = PREFERENCES.get(key)
    if spec is None:
        raise ValidationError({key: "Unknown preference."})
    if not isinstance(value, str):
        raise ValidationError({key: "Must be text."})
    cleaned = value.strip()
    if key == "language":
        cleaned = cleaned.lower()
    if not cleaned or len(cleaned) > spec.max_length:
        raise ValidationError({key: f"Enter a value of up to {spec.max_length} characters."})
    try:
        spec.validator(cleaned)
    except ValidationError as exc:
        raise ValidationError({key: exc.messages}) from exc
    return cleaned