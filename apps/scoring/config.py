"""The scoring configuration as data: parsing, validation and the canonical (hashable) form.  Pure - no database access.

A configuration is a handful of scalar *settings* plus one JSON *document* (``items``, ``sections``, ``bands``, ``critical``).  The
document is declarative: keys are stable item / section codes and option values, numbers are exact decimal strings.  Nothing in it is
ever evaluated as code.  :func:`validate` checks it against the version's snapshot (the questions, options and types that really
exist) and returns actionable issues; :func:`parse` turns a *valid* document into the immutable :class:`ConfigData` the engine uses.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any

from . import constants as k

DOCUMENT_KEYS = ("items", "sections", "bands", "critical")


class ConfigError(ValueError):
    """The document cannot be parsed at all (malformed); ``validate`` reports it as an issue instead of raising."""


@dataclass(frozen=True)
class Issue:
    level: str  # error | warning
    code: str
    message: str
    subject: str  # e.g. "item:TEMP-001", "section:S-001", "bands", "settings"

    def as_dict(self) -> dict[str, str]:
        return {"level": self.level, "code": self.code, "message": self.message, "subject": self.subject}


@dataclass(frozen=True)
class Range:
    minimum: Decimal | None
    maximum: Decimal | None
    min_inclusive: bool
    max_inclusive: bool
    points: Decimal

    def contains(self, value: Decimal) -> bool:
        if self.minimum is not None and (value < self.minimum or (value == self.minimum and not self.min_inclusive)):
            return False
        if self.maximum is not None and (value > self.maximum or (value == self.maximum and not self.max_inclusive)):
            return False
        return True


@dataclass(frozen=True)
class ItemRule:
    code: str
    weight: Decimal
    points: dict[str, Decimal] = field(default_factory=dict)
    aggregate: str = "sum"
    ranges: tuple[Range, ...] = ()


@dataclass(frozen=True)
class Band:
    minimum: Decimal
    grade: str
    label: str


@dataclass(frozen=True)
class CriticalRule:
    item: str
    op: str
    value: str
    label: str


@dataclass(frozen=True)
class ConfigData:
    enabled: bool
    method: str
    na_policy: str
    missing_policy: str
    precision: int
    critical_overrides_grade: bool
    critical_label: str
    items: dict[str, ItemRule]
    sections: dict[str, Decimal]
    bands: tuple[Band, ...]
    critical: tuple[CriticalRule, ...]


# ---------------------------------------------------------------------------
# Number helpers
# ---------------------------------------------------------------------------
def to_decimal(value: Any, what: str = "number") -> Decimal:
    """An exact, finite decimal from a string / int / Decimal (a ``float`` or ``bool`` is refused: binary floats are not exact)."""
    if isinstance(value, bool) or isinstance(value, float) or value is None:
        raise ConfigError(f"{what} must be a decimal number written as text or an integer.")
    try:
        out = Decimal(str(value).strip())
    except (InvalidOperation, ValueError) as exc:
        raise ConfigError(f"{what} “{value}” is not a number.") from exc
    if not out.is_finite():
        raise ConfigError(f"{what} must be finite.")
    return out


def dstr(value: Decimal | None) -> str | None:
    """Canonical text of a decimal (no exponent, no trailing zeros)."""
    if value is None:
        return None
    text = format(value.normalize(), "f")
    return "0" if text in ("-0", "") else text


# ---------------------------------------------------------------------------
# Defaults, normalisation, hashing
# ---------------------------------------------------------------------------
def default_settings() -> dict[str, Any]:
    return {"enabled": True, "method": k.Method.PERCENTAGE, "na_policy": k.NaPolicy.EXCLUDE, "missing_policy": k.MissingPolicy.ZERO,
            "precision": 2, "critical_overrides_grade": False, "critical_label": ""}


def empty_document() -> dict[str, Any]:
    return {"items": {}, "sections": {}, "bands": [], "critical": []}


def canonical(settings: dict[str, Any], document: dict[str, Any]) -> dict[str, Any]:
    """The deterministic description sealed by the version's integrity hash (sorted keys; lists keep their meaningful order)."""
    return {"settings": {key: settings[key] for key in sorted(settings)}, "document": {key: document.get(key) for key in DOCUMENT_KEYS}}


def config_hash(settings: dict[str, Any], document: dict[str, Any]) -> str:
    text = json.dumps(canonical(settings, document), sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Parsing (document -> ConfigData); raises ConfigError on a malformed document
# ---------------------------------------------------------------------------
def _parse_range(raw: Any, where: str) -> Range:
    if not isinstance(raw, dict):
        raise ConfigError(f"{where}: a range must be an object.")
    unknown = set(raw) - {"min", "max", "min_inclusive", "max_inclusive", "points"}
    if unknown:
        raise ConfigError(f"{where}: unknown range keys {sorted(unknown)}.")
    lo = None if raw.get("min") in (None, "") else to_decimal(raw["min"], f"{where} minimum")
    hi = None if raw.get("max") in (None, "") else to_decimal(raw["max"], f"{where} maximum")
    if lo is not None and hi is not None and lo > hi:
        raise ConfigError(f"{where}: the minimum is above the maximum.")
    if "points" not in raw:
        raise ConfigError(f"{where}: points are missing.")
    return Range(lo, hi, bool(raw.get("min_inclusive", True)), bool(raw.get("max_inclusive", True)), to_decimal(raw["points"], f"{where} points"))


def _section(document: dict[str, Any], key: str, default: Any) -> Any:
    """``document[key]`` or ``default`` when absent/null; a value of the wrong type is kept so the caller can refuse it (``[]`` is not ``{}``)."""
    value = document.get(key)
    return default if value is None else value


def parse(settings: dict[str, Any], document: dict[str, Any]) -> ConfigData:
    if not isinstance(document, dict):
        raise ConfigError("The scoring document must be an object.")
    unknown = set(document) - set(DOCUMENT_KEYS)
    if unknown:
        raise ConfigError(f"Unknown scoring document keys: {sorted(unknown)}.")
    items: dict[str, ItemRule] = {}
    raw_items = _section(document, "items", {})
    if not isinstance(raw_items, dict):
        raise ConfigError("“items” must be an object keyed by item code.")
    for code, raw in raw_items.items():
        where = f"item {code}"
        if not isinstance(raw, dict):
            raise ConfigError(f"{where}: the rule must be an object.")
        extra = set(raw) - {"weight", "points", "aggregate", "ranges"}
        if extra:
            raise ConfigError(f"{where}: unknown keys {sorted(extra)}.")
        points_raw = raw.get("points") or {}
        if not isinstance(points_raw, dict):
            raise ConfigError(f"{where}: points must be an object.")
        ranges_raw = raw.get("ranges") or []
        if not isinstance(ranges_raw, list):
            raise ConfigError(f"{where}: ranges must be a list.")
        items[code] = ItemRule(
            code=code, weight=to_decimal(raw.get("weight", "1"), f"{where} weight"),
            points={str(key): to_decimal(val, f"{where} points for “{key}”") for key, val in points_raw.items()},
            aggregate=str(raw.get("aggregate") or "sum"), ranges=tuple(_parse_range(r, where) for r in ranges_raw),
        )
    raw_sections = _section(document, "sections", {})
    if not isinstance(raw_sections, dict):
        raise ConfigError("“sections” must be an object keyed by section code.")
    sections = {code: to_decimal(weight, f"section {code} weight") for code, weight in raw_sections.items()}
    raw_bands = _section(document, "bands", [])
    if not isinstance(raw_bands, list):
        raise ConfigError("“bands” must be a list.")
    bands = []
    for raw in raw_bands:
        if not isinstance(raw, dict) or set(raw) - {"min", "grade", "label"}:
            raise ConfigError("Each grade band must be an object with min, grade and label.")
        bands.append(Band(to_decimal(raw.get("min"), "grade band minimum"), str(raw.get("grade") or "").strip(), str(raw.get("label") or "").strip()))
    raw_crit = _section(document, "critical", [])
    if not isinstance(raw_crit, list):
        raise ConfigError("“critical” must be a list.")
    critical = []
    for raw in raw_crit:
        if not isinstance(raw, dict) or set(raw) - {"item", "op", "value", "label"}:
            raise ConfigError("Each critical rule must be an object with item, op, value and label.")
        critical.append(CriticalRule(str(raw.get("item") or ""), str(raw.get("op") or ""), str(raw.get("value") if raw.get("value") is not None else ""),
                                     str(raw.get("label") or "").strip()))
    return ConfigData(
        enabled=bool(settings.get("enabled", True)), method=settings["method"], na_policy=settings["na_policy"], missing_policy=settings["missing_policy"],
        precision=int(settings["precision"]), critical_overrides_grade=bool(settings.get("critical_overrides_grade")),
        critical_label=str(settings.get("critical_label") or ""), items=items, sections=sections,
        bands=tuple(sorted(bands, key=lambda b: b.minimum)), critical=tuple(critical),
    )


def to_document(data: ConfigData) -> dict[str, Any]:
    """The normalized document of a parsed configuration (canonical decimal text, sorted keys) - what is stored and sealed."""
    items: dict[str, Any] = {}
    for code in sorted(data.items):
        rule = data.items[code]
        entry: dict[str, Any] = {"weight": dstr(rule.weight)}
        if rule.points:
            entry["points"] = {key: dstr(rule.points[key]) for key in sorted(rule.points)}
        entry["aggregate"] = rule.aggregate
        if rule.ranges:
            entry["ranges"] = [{"min": dstr(r.minimum), "max": dstr(r.maximum), "min_inclusive": r.min_inclusive, "max_inclusive": r.max_inclusive,
                                "points": dstr(r.points)} for r in rule.ranges]
        items[code] = entry
    return {
        "items": items,
        "sections": {code: dstr(data.sections[code]) for code in sorted(data.sections)},
        "bands": [{"min": dstr(b.minimum), "grade": b.grade, "label": b.label} for b in data.bands],
        "critical": [{"item": c.item, "op": c.op, "value": c.value, "label": c.label} for c in data.critical],
    }


# ---------------------------------------------------------------------------
# Item maximum (what an answer could earn at most, before the weight)
# ---------------------------------------------------------------------------
def item_maximum(rule: ItemRule, answer_type: str, type_config: dict[str, Any]) -> Decimal:
    if answer_type == "score":
        try:
            return max(Decimal(str(type_config.get("max", 0))), Decimal(0))
        except (InvalidOperation, ValueError):
            return Decimal(0)
    if answer_type == "numeric":
        return max((r.points for r in rule.ranges), default=Decimal(0))
    values = [p for p in rule.points.values()]
    if not values:
        return Decimal(0)
    if answer_type == "multiple_choice" and rule.aggregate == "sum":
        return sum((p for p in values if p > 0), Decimal(0))
    return max(values)


# ---------------------------------------------------------------------------
# Validation against the version's questions
# ---------------------------------------------------------------------------
def validate(settings: dict[str, Any], document: dict[str, Any], snapshot: Any) -> list[Issue]:
    """Every reason this configuration cannot be published (errors) or deserves attention (warnings).  Never raises."""
    issues: list[Issue] = []

    def err(code: str, message: str, subject: str) -> None:
        issues.append(Issue("error", code, message, subject))

    def warn(code: str, message: str, subject: str) -> None:
        issues.append(Issue("warning", code, message, subject))

    if settings.get("method") not in k.Method.values:
        err("invalid_method", "The scoring method is not supported.", "settings")
    if settings.get("na_policy") not in k.NaPolicy.values:
        err("invalid_na_policy", "The N/A policy is not supported.", "settings")
    if settings.get("missing_policy") not in k.MissingPolicy.values:
        err("invalid_missing_policy", "The missing-answer policy is not supported.", "settings")
    precision = settings.get("precision")
    if not isinstance(precision, int) or isinstance(precision, bool) or not 0 <= precision <= 4:
        err("invalid_precision", "The rounding precision must be a whole number from 0 to 4.", "settings")
    if issues:
        return issues
    try:
        data = parse(settings, document)
    except ConfigError as exc:
        return [Issue("error", "malformed", str(exc), "document")]
    if not data.enabled:
        return []  # scoring switched off: nothing else is evaluated (and the version publishes without a score)

    if len(data.items) > k.MAX_ITEMS or len(data.bands) > k.MAX_BANDS or len(data.critical) > k.MAX_CRITICAL:
        err("too_large", "The scoring configuration exceeds the supported size.", "document")
        return issues
    if not data.items:
        err("no_scored_items", "Scoring is enabled but no question is scored.", "items")

    for code, rule in data.items.items():
        subject = f"item:{code}"
        spec = snapshot.items.get(code)
        if spec is None:
            err("unknown_item", f"Scoring refers to question {code}, which does not exist in this version.", subject)
            continue
        if not spec.active:
            warn("inactive_item", f"Question {code} is inactive; its scoring rule is never used.", subject)
        if not Decimal(0) <= rule.weight <= k.MAX_WEIGHT:
            err("invalid_weight", f"Question {code}: the weight must be from 0 to {k.MAX_WEIGHT}.", subject)
        if spec.answer_type not in k.SCORABLE_TYPES:
            err("unsupported_type", f"Question {code} is a “{spec.answer_type}” question; this answer type cannot be scored.", subject)
            continue
        _validate_mapping(rule, spec, subject, err, warn)

    for code, weight in data.sections.items():
        subject = f"section:{code}"
        if code not in snapshot.sections:
            err("unknown_section", f"Scoring refers to section {code}, which does not exist in this version.", subject)
        if not Decimal(0) <= weight <= k.MAX_WEIGHT:
            err("invalid_weight", f"Section {code}: the weight must be from 0 to {k.MAX_WEIGHT}.", subject)
    if data.method == k.Method.WEIGHTED_SECTIONS:
        scored_sections = {snapshot.items[c].section_code for c in data.items if c in snapshot.items}
        if scored_sections and not any(data.sections.get(s, Decimal(1)) > 0 for s in scored_sections):
            err("no_section_weight", "Every scored section has a weight of zero; the weighted result would have no denominator.", "sections")

    _validate_bands(data, err, warn)
    _validate_critical(data, snapshot, err)
    return issues


def _validate_mapping(rule: ItemRule, spec: Any, subject: str, err: Any, warn: Any) -> None:
    code, atype = rule.code, spec.answer_type
    for key, value in rule.points.items():
        if not Decimal(0) <= value <= k.MAX_POINTS:
            err("invalid_points", f"Question {code}: points for “{key}” must be from 0 to {k.MAX_POINTS} (penalties are not supported).", subject)
    if atype in k.FIXED_POINT_KEYS:
        wanted = set(k.FIXED_POINT_KEYS[atype])
        missing, extra = wanted - set(rule.points), set(rule.points) - wanted
        if missing:
            err("missing_mapping", f"Question {code}: give points for {', '.join(sorted(missing))}.", subject)
        if extra:
            err("unknown_option", f"Question {code}: “{', '.join(sorted(extra))}” is not an answer of this question"
                + (" (N/A is handled by the N/A policy, not by points)." if atype == "yes_no_na" else "."), subject)
    elif atype in k.CHOICE_TYPES:
        active = [o.value for o in spec.options if o.is_active]
        known = {o.value for o in spec.options}
        missing = [v for v in active if v not in rule.points]
        extra = [v for v in rule.points if v not in known]
        if missing:
            err("missing_mapping", f"Question {code}: give points for every option (missing: {', '.join(missing)}).", subject)
        if extra:
            err("unknown_option", f"Question {code}: “{', '.join(extra)}” is not an option of this question.", subject)
        if atype == "multiple_choice" and rule.aggregate not in ("sum", "max"):
            err("invalid_aggregate", f"Question {code}: a multiple-choice rule must aggregate by “sum” or “max”.", subject)
    elif atype == "numeric":
        _validate_ranges(rule, spec, subject, err, warn)
    elif atype == "score":
        if rule.points or rule.ranges:
            err("unexpected_mapping", f"Question {code} is a score question: the answer itself is the score; remove the mapping.", subject)
        if Decimal(str(spec.config.get("max", 0))) <= 0:
            err("no_maximum", f"Question {code}: the score question has no positive maximum.", subject)
    if atype != "numeric" and rule.ranges:
        err("unexpected_ranges", f"Question {code}: ranges apply to numeric questions only.", subject)
    if atype in k.FIXED_POINT_KEYS or atype in k.CHOICE_TYPES:
        if all(p == 0 for p in rule.points.values()) and rule.points:
            warn("all_zero", f"Question {code}: every answer earns zero points.", subject)


def _validate_ranges(rule: ItemRule, spec: Any, subject: str, err: Any, warn: Any) -> None:
    code = rule.code
    if rule.points:
        err("unexpected_mapping", f"Question {code}: a numeric question is scored with ranges, not option points.", subject)
    if not rule.ranges:
        err("no_ranges", f"Question {code}: add at least one range.", subject)
        return
    if len(rule.ranges) > k.MAX_RANGES:
        err("too_many_ranges", f"Question {code}: at most {k.MAX_RANGES} ranges are supported.", subject)
        return
    for r in rule.ranges:
        if not Decimal(0) <= r.points <= k.MAX_POINTS:
            err("invalid_points", f"Question {code}: range points must be from 0 to {k.MAX_POINTS}.", subject)
        if r.minimum is not None and r.maximum is not None and r.minimum == r.maximum and not (r.min_inclusive and r.max_inclusive):
            err("empty_range", f"Question {code}: a range from {dstr(r.minimum)} to {dstr(r.maximum)} contains no value.", subject)
    ordered = sorted(rule.ranges, key=lambda r: (r.minimum is not None, r.minimum if r.minimum is not None else Decimal(0), r.min_inclusive is False))
    for first, second in zip(ordered, ordered[1:], strict=False):
        if first.maximum is None or second.minimum is None:
            err("overlapping_ranges", f"Question {code}: an open-ended range overlaps another range.", subject)
            return
        if first.maximum > second.minimum or (first.maximum == second.minimum and first.max_inclusive and second.min_inclusive):
            err("overlapping_ranges", f"Question {code}: the ranges ending at {dstr(first.maximum)} and starting at {dstr(second.minimum)} overlap.", subject)
            return
        if first.maximum < second.minimum or not (first.max_inclusive or second.min_inclusive):
            _gap(rule, spec, subject, err, warn, f"between {dstr(first.maximum)} and {dstr(second.minimum)}")
    lo, hi = spec.config.get("min"), spec.config.get("max")
    first, last = ordered[0], ordered[-1]
    if lo is not None and first.minimum is not None:
        low = Decimal(str(lo))
        if low < first.minimum or (low == first.minimum and not first.min_inclusive):
            _gap(rule, spec, subject, err, warn, f"below {dstr(first.minimum)}")
    if hi is not None and last.maximum is not None:
        high = Decimal(str(hi))
        if high > last.maximum or (high == last.maximum and not last.max_inclusive):
            _gap(rule, spec, subject, err, warn, f"above {dstr(last.maximum)}")
    if lo is None or hi is None:
        warn("open_ended_question", f"Question {code} has no minimum or maximum, so a value outside every range is possible; it is excluded and flagged.", subject)


def _gap(rule: ItemRule, spec: Any, subject: str, err: Any, warn: Any, where: str) -> None:
    bounded = spec.config.get("min") is not None and spec.config.get("max") is not None
    (err if bounded else warn)("range_gap", f"Question {rule.code}: the ranges leave values {where} without points.", subject)


def _validate_bands(data: ConfigData, err: Any, warn: Any) -> None:
    if not data.bands:
        warn("no_bands", "No grade bands are configured; results show a percentage without a grade.", "bands")
        return
    seen: set[Decimal] = set()
    for band in data.bands:
        if not Decimal(0) <= band.minimum <= 100:
            err("invalid_band", "A grade band minimum must be from 0 to 100.", "bands")
        if band.minimum in seen:
            err("duplicate_band", f"Two grade bands start at {dstr(band.minimum)}.", "bands")
        seen.add(band.minimum)
        if not band.grade or len(band.grade) > 20:
            err("invalid_grade", "Every grade band needs a grade of 1-20 characters.", "bands")
        if len(band.label) > 100:
            err("invalid_label", "A grade label can have at most 100 characters.", "bands")
    if Decimal(0) not in seen:
        err("bands_do_not_cover", "The lowest grade band must start at 0 so every percentage has a grade.", "bands")


def _validate_critical(data: ConfigData, snapshot: Any, err: Any) -> None:
    for index, rule in enumerate(data.critical, 1):
        subject = f"critical:{index}"
        spec = snapshot.items.get(rule.item)
        if spec is None:
            err("unknown_item", f"Critical rule {index} refers to question {rule.item}, which does not exist in this version.", subject)
            continue
        if rule.item not in data.items:
            err("critical_item_not_scored", f"Critical rule {index}: question {rule.item} has no scoring rule.", subject)
        if rule.op not in k.CriticalOp.values:
            err("invalid_critical_op", f"Critical rule {index}: the condition “{rule.op}” is not supported.", subject)
            continue
        if not rule.label or len(rule.label) > 100:
            err("invalid_label", f"Critical rule {index} needs a label of 1-100 characters.", subject)
        if rule.op == k.CriticalOp.POINTS_BELOW:
            try:
                to_decimal(rule.value, "value")
            except ConfigError:
                err("invalid_critical_value", f"Critical rule {index}: the points threshold must be a number.", subject)
        elif spec.answer_type in k.FIXED_POINT_KEYS:
            if rule.value not in k.FIXED_POINT_KEYS[spec.answer_type]:
                err("invalid_critical_value", f"Critical rule {index}: “{rule.value}” is not an answer of question {rule.item}.", subject)
        elif spec.answer_type in k.CHOICE_TYPES:
            if rule.value not in {o.value for o in spec.options}:
                err("invalid_critical_value", f"Critical rule {index}: “{rule.value}” is not an option of question {rule.item}.", subject)
        else:
            try:
                to_decimal(rule.value, "value")
            except ConfigError:
                err("invalid_critical_value", f"Critical rule {index}: the value must be a number.", subject)
    if data.critical_overrides_grade and not data.critical_label:
        err("missing_critical_label", "A critical failure overrides the grade, so the outcome label is required.", "settings")
