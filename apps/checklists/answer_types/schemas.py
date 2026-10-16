"""Configuration schemas: each answer type declares its settings as data; one generic cleaner enforces them.

Why a schema instead of columns or free JSON: the allowed keys, types, ranges and defaults live in one
declaration per type.  The same declaration drives the validation (``clean_config``), the builder form
(dynamic fields), the API metadata and the documentation - so they can never disagree.

Kinds: ``int`` and ``number`` (numeric, ``number`` allows decimals), ``text``, ``bool``, ``choice``, ``list``
(of short strings), ``labels`` (keyed labels, e.g. matrix rows), ``date`` and ``datetime`` (ISO 8601 strings).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from django.utils import timezone

from .exceptions import ConfigurationError

_MISSING = object()
_KEY_RE = re.compile(r"[^a-z0-9]+")


@dataclass(frozen=True)
class ConfigField:
    key: str
    label: str
    kind: str
    default: Any = None
    help: str = ""
    min: float | None = None
    max: float | None = None
    max_length: int | None = None  # for text / list items
    max_items: int | None = None  # for list
    choices: tuple[tuple[str, str], ...] = ()
    #: Allowed values for list items (e.g. file extensions); empty = free labels.
    universe: tuple[str, ...] = ()
    #: Render hint for the builder form: "textarea" for multi-line lists.
    widget: str = ""

    def describe(self) -> dict[str, Any]:
        """JSON-safe description for the API and the builder (no internals)."""
        return {
            "key": self.key, "label": self.label, "kind": self.kind, "default": self.default, "help": self.help,
            "min": self.min, "max": self.max, "max_length": self.max_length, "max_items": self.max_items,
            "choices": [{"value": v, "label": label} for v, label in self.choices],
            "allowed_values": list(self.universe),
        }


def matrix_key(label: str) -> str:
    """Stable machine key for a matrix row/column label (``Dining Area`` -> ``dining_area``)."""
    return _KEY_RE.sub("_", label.strip().lower()).strip("_")[:64]


def to_decimal(value: Any, key: str = "") -> Decimal:
    """Parse a number without ever going through binary floating point."""
    if isinstance(value, bool):
        raise ConfigurationError("must be a number.", key)
    if isinstance(value, Decimal):
        result = value
    elif isinstance(value, (int, float)):
        result = Decimal(str(value))
    elif isinstance(value, str):
        try:
            result = Decimal(value.strip())
        except InvalidOperation:
            raise ConfigurationError("must be a number.", key) from None
    else:
        raise ConfigurationError("must be a number.", key)
    if not result.is_finite():
        raise ConfigurationError("must be a finite number.", key)
    return result


def json_number(value: Decimal) -> int | float:
    """Store integral decimals as int and others as the shortest float (``Decimal(str(x))`` round-trips)."""
    return int(value) if value == value.to_integral_value() else float(value)


def parse_iso_date(value: Any, key: str) -> date:
    if isinstance(value, datetime):
        raise ConfigurationError("must be a date (YYYY-MM-DD).", key)
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value).strip())
    except ValueError:
        raise ConfigurationError("must be a date (YYYY-MM-DD).", key) from None


def parse_iso_datetime(value: Any, key: str) -> datetime:
    """An aware datetime in UTC; a naive value is read in the project's current time zone."""
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(str(value).strip())
        except ValueError:
            raise ConfigurationError("must be a date and time (ISO 8601).", key) from None
    if timezone.is_naive(parsed):
        parsed = timezone.make_aware(parsed, timezone.get_current_timezone())
    return parsed.astimezone(UTC)


def _clean_one(spec: ConfigField, value: Any) -> Any:
    key = spec.key
    if spec.kind == "int":
        if isinstance(value, bool) or not isinstance(value, (int, str, float, Decimal)):
            raise ConfigurationError("must be a whole number.", key)
        number = to_decimal(value, key)
        if number != number.to_integral_value():
            raise ConfigurationError("must be a whole number.", key)
        out: Any = int(number)
    elif spec.kind == "number":
        out = to_decimal(value, key)
    elif spec.kind == "text":
        if not isinstance(value, str):
            raise ConfigurationError("must be text.", key)
        out = value.strip()
        if spec.max_length and len(out) > spec.max_length:
            raise ConfigurationError(f"must be at most {spec.max_length} characters.", key)
    elif spec.kind == "bool":
        if not isinstance(value, bool):
            raise ConfigurationError("must be true or false.", key)
        out = value
    elif spec.kind == "choice":
        if value not in {v for v, _ in spec.choices}:
            raise ConfigurationError("must be one of: " + ", ".join(v for v, _ in spec.choices) + ".", key)
        out = value
    elif spec.kind == "list":
        if not isinstance(value, (list, tuple)):
            raise ConfigurationError("must be a list.", key)
        out = []
        for entry in value:
            if not isinstance(entry, str) or not entry.strip():
                raise ConfigurationError("must contain non-empty text entries.", key)
            entry = entry.strip().lower() if spec.universe else entry.strip()
            if spec.max_length and len(entry) > spec.max_length:
                raise ConfigurationError(f"entries must be at most {spec.max_length} characters.", key)
            if spec.universe and entry.lstrip(".") not in spec.universe:
                raise ConfigurationError(f"“{entry}” is not allowed here.", key)
            entry = entry.lstrip(".") if spec.universe else entry
            if entry not in out:
                out.append(entry)
        if spec.max_items and len(out) > spec.max_items:
            raise ConfigurationError(f"may have at most {spec.max_items} entries.", key)
    elif spec.kind == "labels":
        if not isinstance(value, (list, tuple)):
            raise ConfigurationError("must be a list.", key)
        out, seen = [], set()
        for entry in value:
            if isinstance(entry, dict):
                if set(entry) - {"key", "label"}:
                    raise ConfigurationError("entries may only have “key” and “label”.", key)
                label, ident = entry.get("label"), entry.get("key")
            else:
                label, ident = entry, None
            if not isinstance(label, str) or not label.strip() or len(label.strip()) > 120:
                raise ConfigurationError("entries need a label of 1-120 characters.", key)
            ident = ident if isinstance(ident, str) and ident else matrix_key(label)
            if not re.fullmatch(r"[a-z0-9][a-z0-9_]{0,63}", ident):
                raise ConfigurationError("entry keys use lowercase letters, digits and “_”.", key)
            if ident in seen:
                raise ConfigurationError(f"“{label.strip()}” is listed twice.", key)
            seen.add(ident)
            out.append({"key": ident, "label": label.strip()})
        if not 1 <= len(out) <= (spec.max_items or 50):
            raise ConfigurationError(f"needs between 1 and {spec.max_items or 50} entries.", key)
    elif spec.kind == "date":
        out = parse_iso_date(value, key).isoformat()
    elif spec.kind == "datetime":
        out = parse_iso_datetime(value, key).isoformat()
    else:  # pragma: no cover - a schema bug, caught by the registry tests
        raise ConfigurationError(f"unsupported setting kind {spec.kind!r}.", key)
    if spec.kind in ("int", "number"):
        number = Decimal(out)
        if spec.min is not None and number < Decimal(str(spec.min)):
            raise ConfigurationError(f"must be at least {spec.min:g}.", key)
        if spec.max is not None and number > Decimal(str(spec.max)):
            raise ConfigurationError(f"must be at most {spec.max:g}.", key)
        out = int(number) if spec.kind == "int" else number
    return out


def clean_setting(spec: ConfigField, value: Any) -> Any:
    """Validate one value against one setting declaration (used by rule-driven constraints)."""
    return _clean_one(spec, value)


def clean_with_schema(fields: tuple[ConfigField, ...], raw: Any) -> dict[str, Any]:
    """Validate ``raw`` against ``fields``: unknown keys rejected, types checked, defaults filled.

    Numbers of kind ``number`` are returned as :class:`~decimal.Decimal` for the definition's cross-field
    checks; :func:`jsonable` converts them for storage.
    """
    if raw in (None, ""):
        raw = {}
    if not isinstance(raw, dict):
        raise ConfigurationError("Configuration must be an object.")
    known = {f.key: f for f in fields}
    unknown = sorted(set(raw) - set(known))
    if unknown:
        raise ConfigurationError("This answer type does not accept: " + ", ".join(unknown) + ".")
    cleaned: dict[str, Any] = {}
    for spec in fields:
        value = raw.get(spec.key, _MISSING)
        if value is _MISSING or value is None or value == "":
            if spec.default is not None:
                cleaned[spec.key] = spec.default
            continue
        try:
            cleaned[spec.key] = _clean_one(spec, value)
        except ConfigurationError as exc:
            raise ConfigurationError(f"“{spec.label}” {exc.message}", spec.key) from None
    return cleaned


def jsonable(config: dict[str, Any]) -> dict[str, Any]:
    """Convert a cleaned config to JSON-storable values (Decimals -> int/float)."""
    return {k: json_number(v) if isinstance(v, Decimal) else v for k, v in config.items()}
