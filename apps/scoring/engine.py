"""The one authoritative score calculation.  Pure: no database, no clock, no randomness - the same inputs always give the same result.

``calculate(config, facts, final=...)`` is used by the live preview, the designer's what-if, the execution summary, the submission
snapshot and the tests.  Nothing else computes a score.

Formulas (all arithmetic is exact ``Decimal``; only the percentage is rounded, ROUND_HALF_UP, to the configured precision)
-----------------------------------------------------------------------------------------------------------------------
For every *scored, included* question:  ``earned = points x weight`` and ``available = item maximum x weight``.

* ``percentage`` / ``raw_total``:  ``percentage = sum(earned) / sum(available) x 100``.  ``raw_total`` only changes which figure
  is the headline (points instead of the percentage).
* ``weighted_sections``:  per section ``p_s = sum(earned_s) / sum(available_s)``; overall
  ``percentage = sum(p_s x w_s) / sum(w_s) x 100`` over the sections that have available points (``w_s`` = section weight, 1 when
  unset).  Question weights act only *inside* a section, section weights only *between* sections - nothing is weighted twice.
* No available points (or no section weight) -> status ``not_applicable``; never 0 %.

Inclusion: a hidden (rule-hidden or inactive) question is always excluded; N/A follows ``na_policy``; an unanswered question follows
``missing_policy`` **only when the result is final** (a draft is provisional and excludes it); an invalid answer is excluded and
flagged - it is never silently scored as zero.  Critical rules are evaluated on visible, answered questions and never change the
numbers; they may replace the displayed label when the configuration says so.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from . import constants as k
from .config import ConfigData, ItemRule, dstr, item_maximum

POINT_PLACES = Decimal("0.0001")


@dataclass(frozen=True)
class ItemFacts:
    """What the engine knows about one question's answer (built from the execution's evaluated state, or from sample data)."""

    code: str
    section: str
    question: str
    answer_type: str
    type_config: dict[str, Any]
    visible: bool
    status: str  # answered | empty | not_applicable | invalid | hidden   (the Phase 6 answer states)
    value: Any = None
    labels: dict[str, str] = field(default_factory=dict)  # option value -> label (for readable summaries)


@dataclass
class ItemScore:
    code: str
    section: str
    question: str
    answer: str
    included: bool
    reason: str  # scored | hidden | not_applicable | unanswered | invalid | no_matching_range | zero_weight
    points: Decimal | None
    weight: Decimal
    earned: Decimal
    available: Decimal
    explanation: str

    def as_dict(self) -> dict[str, Any]:
        return {"code": self.code, "section": self.section, "question": self.question, "answer": self.answer, "included": self.included,
                "reason": self.reason, "points": dstr(self.points), "weight": dstr(self.weight), "earned": dstr(self.earned),
                "available": dstr(self.available), "explanation": self.explanation}


@dataclass
class SectionScore:
    code: str
    name: str
    weight: Decimal
    earned: Decimal
    available: Decimal
    percentage: Decimal | None
    included: int
    excluded: int

    def as_dict(self) -> dict[str, Any]:
        return {"code": self.code, "name": self.name, "weight": dstr(self.weight), "earned": dstr(self.earned), "available": dstr(self.available),
                "percentage": dstr(self.percentage), "included": self.included, "excluded": self.excluded}


@dataclass
class ScoreOutcome:
    status: str
    method: str
    earned: Decimal | None
    available: Decimal | None
    percentage: Decimal | None
    grade: str
    label: str
    display_label: str
    critical: bool
    critical_hits: list[dict[str, str]]
    items: list[ItemScore]
    sections: list[SectionScore]
    warnings: list[str]
    explanation: list[str]
    algorithm: int = k.ALGORITHM_VERSION

    @property
    def excluded(self) -> list[ItemScore]:
        return [i for i in self.items if not i.included]

    def breakdown(self) -> dict[str, Any]:
        return {"items": [i.as_dict() for i in self.items], "sections": [s.as_dict() for s in self.sections], "warnings": self.warnings,
                "explanation": self.explanation, "critical_hits": self.critical_hits}

    def as_dict(self) -> dict[str, Any]:
        return {"status": self.status, "method": self.method, "earned": dstr(self.earned), "available": dstr(self.available),
                "percentage": dstr(self.percentage), "grade": self.grade, "label": self.label, "display_label": self.display_label,
                "critical": self.critical, "algorithm": self.algorithm, **self.breakdown()}


def quantize(value: Decimal, places: int) -> Decimal:
    return value.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)


def _points(value: Decimal) -> Decimal:
    return value.quantize(POINT_PLACES, rounding=ROUND_HALF_UP)


def input_fingerprint(facts: list[ItemFacts]) -> str:
    """A stable digest of exactly the answers that were scored (visibility, status and normalized value), in question-code order."""
    rows = [[f.code, f.visible, f.status, json.dumps(f.value, sort_keys=True, default=str)] for f in sorted(facts, key=lambda f: f.code)]
    return hashlib.sha256(json.dumps(rows, separators=(",", ":")).encode("utf-8")).hexdigest()


def _summary(f: ItemFacts) -> str:
    """A safe, short description of the answer (option labels instead of raw values; no free text is ever copied)."""
    if f.status != "answered":
        return {"not_applicable": "N/A", "empty": "Not answered", "invalid": "Invalid answer", "hidden": "Hidden"}.get(f.status, f.status)
    value = f.value
    if isinstance(value, list):
        return ", ".join(f.labels.get(str(v), str(v)) for v in value) or "(none)"
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if f.answer_type in ("yes_no", "yes_no_na"):
        return {"yes": "Yes", "no": "No", "na": "N/A"}.get(str(value), str(value))
    return f.labels.get(str(value), str(value))


def _points_for(rule: ItemRule, f: ItemFacts) -> tuple[Decimal | None, str]:
    """``(points, "")`` or ``(None, reason)`` when the answer cannot be mapped (never defaults to zero)."""
    value, atype = f.value, f.answer_type
    try:
        if atype == "boolean":
            return rule.points["true" if bool(value) else "false"], ""
        if atype in ("yes_no", "yes_no_na"):
            return rule.points[str(value)], ""
        if atype == "single_choice":
            return rule.points[str(value)], ""
        if atype == "multiple_choice":
            chosen = [rule.points[str(v)] for v in (value or [])]
            if not chosen:
                return Decimal(0), ""
            return (sum(chosen, Decimal(0)) if rule.aggregate == "sum" else max(chosen)), ""
        if atype == "numeric":
            number = Decimal(str(value))
            for r in rule.ranges:
                if r.contains(number):
                    return r.points, ""
            return None, "no_matching_range"
        if atype == "score":
            return Decimal(str(value)), ""
    except (KeyError, ValueError, ArithmeticError):
        return None, "invalid"
    return None, "invalid"


def _critical_hits(config: ConfigData, facts: dict[str, ItemFacts], scored: dict[str, ItemScore]) -> list[dict[str, str]]:
    hits = []
    for rule in config.critical:
        f = facts.get(rule.item)
        if f is None or not f.visible or f.status != "answered":
            continue
        hit = False
        if rule.op in (k.CriticalOp.EQUALS, k.CriticalOp.NOT_EQUALS):
            value = f.value
            if isinstance(value, bool):
                text = "true" if value else "false"
            else:
                text = str(value)
            matched = rule.value in [str(v) for v in value] if isinstance(value, list) else text == rule.value
            hit = matched if rule.op == k.CriticalOp.EQUALS else not matched
        elif rule.op == k.CriticalOp.POINTS_BELOW:
            item = scored.get(rule.item)
            if item is not None and item.included and item.points is not None:
                hit = item.points < Decimal(rule.value)
        if hit:
            hits.append({"item": rule.item, "label": rule.label, "rule": rule.op})
    return hits


def calculate(config: ConfigData, facts_list: list[ItemFacts], *, section_names: dict[str, str] | None = None, final: bool = False) -> ScoreOutcome:
    """Score ``facts_list`` under ``config``.  ``final`` decides whether unanswered questions follow the missing policy (final) or are
    excluded and make the result provisional (draft)."""
    names = section_names or {}
    facts = {f.code: f for f in facts_list}
    warnings: list[str] = []
    scored: dict[str, ItemScore] = {}
    incomplete = False
    for f in facts_list:  # the checklist's own question order - never the (storage-dependent) key order of the document
        code = f.code
        rule = config.items.get(code)
        if rule is None:
            continue  # not a scored question
        maximum = item_maximum(rule, f.answer_type, f.type_config)
        weight = rule.weight
        available = _points(maximum * weight)
        zero = Decimal(0)

        def make(included: bool, reason: str, points: Decimal | None, text: str, *, f: ItemFacts = f, weight: Decimal = weight, available: Decimal = available) -> ItemScore:
            earned = _points(points * weight) if (included and points is not None) else zero
            return ItemScore(f.code, f.section, f.question, _summary(f), included, reason, points, weight, earned, available if included else zero, text)

        if not f.visible or f.status == "hidden":
            scored[code] = make(False, "hidden", None, "Excluded: the question is hidden by the checklist rules.")
        elif f.status == "not_applicable" or (f.status == "answered" and f.answer_type == "yes_no_na" and str(f.value) == "na"):
            if config.na_policy == k.NaPolicy.ZERO:
                scored[code] = make(True, "not_applicable", Decimal(0), f"Answered N/A and counted as zero out of {dstr(available)} available points.")
            else:
                scored[code] = make(False, "not_applicable", None, "Excluded: answered N/A.")
        elif f.status == "empty":
            incomplete = True
            if final and config.missing_policy == k.MissingPolicy.ZERO:
                scored[code] = make(True, "unanswered", Decimal(0), f"Not answered and counted as zero out of {dstr(available)} available points.")
            else:
                scored[code] = make(False, "unanswered", None, "Excluded: not answered" + ("" if final else " yet."))
        elif f.status == "answered":
            points, problem = _points_for(rule, f)
            if points is None:
                incomplete = True
                scored[code] = make(False, problem or "invalid", None, "Excluded: the answer could not be mapped to points" + (" (no matching range)." if problem == "no_matching_range" else "."))
                warnings.append(f"Question {code}: the answer could not be scored and was excluded.")
            else:
                earned = _points(points * weight)
                scored[code] = make(True, "scored", points,
                                    f"Earned {dstr(points)} of {dstr(maximum)} points" + (f"; weight {dstr(weight)} gives {dstr(earned)} of {dstr(available)}." if weight != 1 else "."))
        else:  # invalid
            incomplete = True
            scored[code] = make(False, "invalid", None, "Excluded: the answer is invalid.")
            warnings.append(f"Question {code}: the answer is invalid and was excluded.")

    # -- sections ------------------------------------------------------------------------------------------------------------------
    order: list[str] = []
    for item in scored.values():
        if item.section not in order:
            order.append(item.section)
    sections: list[SectionScore] = []
    for code in order:
        mine = [i for i in scored.values() if i.section == code]
        earned = sum((i.earned for i in mine if i.included), Decimal(0))
        available = sum((i.available for i in mine if i.included), Decimal(0))
        weight = config.sections.get(code, Decimal(1))
        percentage = (earned / available * 100) if available > 0 else None
        sections.append(SectionScore(code, names.get(code, code), weight, earned, available, None if percentage is None else quantize(percentage, config.precision),
                                     sum(1 for i in mine if i.included), sum(1 for i in mine if not i.included)))

    total_earned = sum((s.earned for s in sections), Decimal(0))
    total_available = sum((s.available for s in sections), Decimal(0))
    percentage: Decimal | None = None
    explanation: list[str] = []
    if config.method == k.Method.WEIGHTED_SECTIONS:
        counted = [s for s in sections if s.available > 0 and s.weight > 0]
        denominator = sum((s.weight for s in counted), Decimal(0))
        if counted and denominator > 0:
            raw = sum(((s.earned / s.available) * s.weight for s in counted), Decimal(0)) / denominator * 100
            percentage = quantize(raw, config.precision)
            explanation.append("Weighted sections: " + " + ".join(f"{s.name} {dstr(quantize((s.earned / s.available) * 100, config.precision))}% x weight {dstr(s.weight)}" for s in counted)
                               + f", divided by the total weight {dstr(denominator)}.")
    elif total_available > 0:
        percentage = quantize(total_earned / total_available * 100, config.precision)
        explanation.append(f"Earned {dstr(_points(total_earned))} of {dstr(_points(total_available))} available points = {dstr(percentage)}%.")

    if percentage is None:
        status = k.ResultStatus.NOT_APPLICABLE
        explanation.append("There is nothing to score (no included question has available points), so no score is shown.")
    elif not final or any(i.reason in ("invalid", "no_matching_range") for i in scored.values()):
        status = k.ResultStatus.PROVISIONAL  # a draft, or an answer that could not be scored: never certifiable as final
    else:
        status = k.ResultStatus.FINAL
    if not final and incomplete and percentage is not None:
        explanation.append("Some questions are not answered yet, so this score is provisional.")

    grade = label = ""
    if percentage is not None and config.bands:
        band = max((b for b in config.bands if b.minimum <= percentage), key=lambda b: b.minimum, default=None)
        if band is not None:
            grade, label = band.grade, band.label
            explanation.append(f"{dstr(percentage)}% falls in the band starting at {dstr(band.minimum)}%: grade {band.grade}" + (f" ({band.label})." if band.label else "."))
    hits = _critical_hits(config, facts, scored) if percentage is not None or any(i.included for i in scored.values()) else []
    display = label
    if hits:
        explanation.append("Critical failure: " + "; ".join(h["label"] for h in hits) + ".")
        if config.critical_overrides_grade:
            display = config.critical_label
            explanation.append(f"The configuration replaces the displayed outcome with “{config.critical_label}”; the numeric score is unchanged.")
    return ScoreOutcome(
        status=str(status), method=config.method, earned=None if percentage is None and not total_available else _points(total_earned),
        available=None if not total_available else _points(total_available), percentage=percentage, grade=grade, label=label, display_label=display,
        critical=bool(hits), critical_hits=hits, items=list(scored.values()), sections=sections, warnings=warnings, explanation=explanation,
    )
