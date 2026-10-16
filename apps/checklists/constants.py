"""Vocabulary and answer-type configuration rules for checklist *definitions*.

Phase 4 defines what a checklist asks; it stores no answers.  The answer type of an item is data
(``ChecklistItem.answer_type`` + ``config``), never a separate table, so Boolean, score, numeric, text,
file and matrix items live in one schema.  Phase 5 interprets these definitions to build the answer
engine; here we only validate that the configuration is well formed.
"""

from __future__ import annotations

from typing import Any

from django.core.exceptions import ValidationError
from django.db import models
from django.utils.translation import gettext_lazy as _


class TemplateStatus(models.TextChoices):
    DRAFT = "draft", _("Draft")
    ACTIVE = "active", _("Active")
    ARCHIVED = "archived", _("Archived")


class VersionStatus(models.TextChoices):
    DRAFT = "draft", _("Draft")
    PUBLISHED = "published", _("Published")


class AnswerType(models.TextChoices):
    BOOLEAN = "boolean", _("Boolean (true / false)")
    YES_NO = "yes_no", _("Yes / No")
    YES_NO_NA = "yes_no_na", _("Yes / No / N/A")
    SINGLE_CHOICE = "single_choice", _("Single choice")
    MULTIPLE_CHOICE = "multiple_choice", _("Multiple choice")
    SCORE = "score", _("Score (range)")
    NUMERIC = "numeric", _("Numeric value")
    TEXT = "text", _("Short text")
    LONG_TEXT = "long_text", _("Long text")
    DATE = "date", _("Date")
    DATETIME = "datetime", _("Date and time")
    PHOTO = "photo", _("Photo")
    FILE = "file", _("File")
    SIGNATURE = "signature", _("Signature")
    MATRIX = "matrix", _("Matrix (grid)")


#: Types whose options are fixed by the platform (created automatically, not editable).
FIXED_OPTIONS: dict[str, tuple[tuple[str, str], ...]] = {
    AnswerType.YES_NO: (("yes", "Yes"), ("no", "No")),
    AnswerType.YES_NO_NA: (("yes", "Yes"), ("no", "No"), ("na", "N/A")),
}
#: Types whose options the author defines.
CUSTOM_OPTIONS = frozenset({AnswerType.SINGLE_CHOICE, AnswerType.MULTIPLE_CHOICE})
#: Types that carry options of either kind.
OPTION_TYPES = CUSTOM_OPTIONS | frozenset(FIXED_OPTIONS)

# Rule vocabulary (Phase 6 evaluates these; Phase 4 only stores well-formed rules).
class RuleOperator(models.TextChoices):
    EQUALS = "equals", _("equals")
    NOT_EQUALS = "not_equals", _("does not equal")
    IN = "in", _("is one of")
    GT = "gt", _("is greater than")
    GTE = "gte", _("is at least")
    LT = "lt", _("is less than")
    LTE = "lte", _("is at most")
    ANSWERED = "answered", _("is answered")


class RuleAction(models.TextChoices):
    REQUIRE_COMMENT = "require_comment", _("Require a comment")
    REQUIRE_PHOTO = "require_photo", _("Require a photo")
    SHOW_ITEM = "show_item", _("Show an item")
    SHOW_SECTION = "show_section", _("Show a section")


TARGET_ACTIONS = {RuleAction.SHOW_ITEM: "item", RuleAction.SHOW_SECTION: "section"}


def _number(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValidationError({"config": _("“%(name)s” must be a number.") % {"name": name}})
    return value


def clean_config(answer_type: str, config: Any) -> dict[str, Any]:
    """Validate and normalise ``config`` for ``answer_type``; unknown keys are rejected.

    * ``score``  - ``min`` and ``max`` integers, ``0 <= min < max <= 100`` (default 1-5).
    * ``numeric`` - optional ``min``, ``max`` (min <= max), ``unit`` (<= 20 chars), ``decimals`` (0-6).
    * ``text`` / ``long_text`` - optional ``max_length`` (1-10000).
    * ``matrix`` - ``rows`` and ``columns``: lists of 1-50 non-empty labels (a reserved shape).
    * every other type takes no configuration.
    """
    if config in (None, ""):
        config = {}
    if not isinstance(config, dict):
        raise ValidationError({"config": _("Configuration must be an object.")})
    config = dict(config)
    allowed: dict[str, set[str]] = {
        AnswerType.SCORE: {"min", "max"},
        AnswerType.NUMERIC: {"min", "max", "unit", "decimals"},
        AnswerType.TEXT: {"max_length"},
        AnswerType.LONG_TEXT: {"max_length"},
        AnswerType.MATRIX: {"rows", "columns"},
    }
    unknown = set(config) - allowed.get(answer_type, set())
    if unknown:
        raise ValidationError(
            {"config": _("This answer type does not accept: %(keys)s.") % {"keys": ", ".join(sorted(unknown))}}
        )
    if answer_type == AnswerType.SCORE:
        low, high = config.get("min", 1), config.get("max", 5)
        if not all(isinstance(v, int) and not isinstance(v, bool) for v in (low, high)):
            raise ValidationError({"config": _("Score minimum and maximum must be whole numbers.")})
        if not 0 <= low < high <= 100:
            raise ValidationError({"config": _("Score range must satisfy 0 ≤ minimum < maximum ≤ 100.")})
        return {"min": low, "max": high}
    if answer_type == AnswerType.NUMERIC:
        clean: dict[str, Any] = {}
        for key in ("min", "max"):
            if config.get(key) is not None:
                clean[key] = _number(config[key], key)
        if "min" in clean and "max" in clean and clean["min"] > clean["max"]:
            raise ValidationError({"config": _("The minimum cannot exceed the maximum.")})
        if config.get("unit"):
            if not isinstance(config["unit"], str) or len(config["unit"]) > 20:
                raise ValidationError({"config": _("Unit must be text of at most 20 characters.")})
            clean["unit"] = config["unit"].strip()
        if config.get("decimals") is not None:
            decimals = config["decimals"]
            if not isinstance(decimals, int) or isinstance(decimals, bool) or not 0 <= decimals <= 6:
                raise ValidationError({"config": _("Decimals must be a whole number from 0 to 6.")})
            clean["decimals"] = decimals
        return clean
    if answer_type in (AnswerType.TEXT, AnswerType.LONG_TEXT):
        if config.get("max_length") is None:
            return {}
        length = config["max_length"]
        if not isinstance(length, int) or isinstance(length, bool) or not 1 <= length <= 10000:
            raise ValidationError({"config": _("Maximum length must be a whole number from 1 to 10000.")})
        return {"max_length": length}
    if answer_type == AnswerType.MATRIX:
        clean = {}
        for key in ("rows", "columns"):
            labels = config.get(key, [])
            if (
                not isinstance(labels, list)
                or not 1 <= len(labels) <= 50
                or not all(isinstance(x, str) and x.strip() and len(x) <= 120 for x in labels)
            ):
                raise ValidationError({"config": _("Matrix “%(key)s” must be 1-50 non-empty labels.") % {"key": key}})
            clean[key] = [x.strip() for x in labels]
        return clean
    return {}
