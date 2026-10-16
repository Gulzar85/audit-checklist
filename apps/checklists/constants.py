"""Vocabulary and answer-type configuration rules for checklist *definitions*.

Phase 4 defines what a checklist asks; it stores no answers.  The answer type of an item is data
(``ChecklistItem.answer_type`` + ``config``), never a separate table, so Boolean, score, numeric, text,
file and matrix items live in one schema.  The behaviour of each type - metadata, configuration schema,
validation and the normalized value contract - lives in ``apps.checklists.answer_types`` (Phase 5).
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
    """Lifecycle of one version (ADR 0048): DRAFT -> PUBLISHED -> SUPERSEDED.  Archiving is a *template* status."""

    DRAFT = "draft", _("Draft")
    PUBLISHED = "published", _("Published")
    SUPERSEDED = "superseded", _("Superseded")


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

def clean_config(answer_type: str, config: Any) -> dict[str, Any]:
    """Validate and normalise ``config`` for ``answer_type`` (the single entry point for web, API and imports).

    The rules live in the answer-type registry (``apps.checklists.answer_types``): each type declares its settings
    as a schema, unknown keys are rejected, defaults are filled in.  This wrapper only translates the engine's
    :class:`ConfigurationError` into Django's ``ValidationError`` for the service layer.
    """
    from .answer_types import ConfigurationError, registry

    if answer_type not in registry:
        raise ValidationError({"answer_type": _("Unknown answer type.")})
    try:
        return registry.clean_config(answer_type, config)
    except ConfigurationError as exc:
        raise ValidationError({"config": exc.message}) from exc
