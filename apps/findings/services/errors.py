"""Errors of the finding services.  Django ``ValidationError`` subclasses, so every channel (web, API, commands) can show them."""

from __future__ import annotations

from typing import Any

from django.core.exceptions import ValidationError


class FindingError(ValidationError):
    """A business rule refused the operation; ``finding_code`` says which (``invalid_transition``, ``reason_required`` ...)."""

    def __init__(self, message: Any, finding_code: str = "finding_error") -> None:
        super().__init__(message, code=finding_code)
        self.finding_code = finding_code


class FindingConflict(FindingError):
    """The request repeats or races with another (already in that state): HTTP 409, nothing changed."""
