"""Vocabularies of the scoring engine (Phase 15).  Everything a scoring configuration may say comes from here - nothing is free text or code."""

from __future__ import annotations

from django.db import models
from django.utils.translation import gettext_lazy as _

#: Bumped whenever the calculation changes in a way that can alter a result; stored on every result so history stays interpretable.
ALGORITHM_VERSION = 1


class Method(models.TextChoices):
    PERCENTAGE = "percentage", _("Percentage of available points")
    RAW_TOTAL = "raw_total", _("Raw total points (percentage also shown)")
    WEIGHTED_SECTIONS = "weighted_sections", _("Weighted sections")


class NaPolicy(models.TextChoices):
    EXCLUDE = "exclude", _("Exclude from the calculation")
    ZERO = "zero", _("Count as zero (points stay available)")


class MissingPolicy(models.TextChoices):
    ZERO = "zero", _("Count as zero (points stay available)")
    EXCLUDE = "exclude", _("Exclude from the calculation")


class ResultStatus(models.TextChoices):
    PROVISIONAL = "provisional", _("Provisional")
    FINAL = "final", _("Final")
    NOT_APPLICABLE = "not_applicable", _("Not applicable")


class ResultKind(models.TextChoices):
    SUBMISSION = "submission", _("Calculated at submission")
    RECALCULATION = "recalculation", _("Authorized recalculation")


class CriticalOp(models.TextChoices):
    EQUALS = "equals", _("Answer equals")
    NOT_EQUALS = "not_equals", _("Answer is not")
    POINTS_BELOW = "points_below", _("Points earned below")


#: Answer types that can be scored; every other type is never given a score (text, dates, media, matrix).
SCORABLE_TYPES = ("boolean", "yes_no", "yes_no_na", "single_choice", "multiple_choice", "numeric", "score")
FIXED_POINT_KEYS = {"boolean": ("true", "false"), "yes_no": ("yes", "no"), "yes_no_na": ("yes", "no")}
CHOICE_TYPES = ("single_choice", "multiple_choice")

MAX_POINTS = 1_000_000
MAX_WEIGHT = 10_000
MAX_ITEMS = 2000
MAX_BANDS = 12
MAX_CRITICAL = 50
MAX_RANGES = 40

#: The documented, finite settings of a configuration (shown by the metadata endpoint).
SETTING_CHOICES = {"method": Method, "na_policy": NaPolicy, "missing_policy": MissingPolicy}
