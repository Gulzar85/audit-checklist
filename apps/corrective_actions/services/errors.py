"""Errors of the corrective-action services.  Django ``ValidationError`` subclasses, so every channel can show them."""

from __future__ import annotations

from typing import Any

from django.core.exceptions import ValidationError


class ActionError(ValidationError):
    """A business rule refused the operation; ``action_code`` says which (``invalid_transition``, ``evidence_required`` ...)."""

    def __init__(self, message: Any, action_code: str = "action_error") -> None:
        super().__init__(message, code=action_code)
        self.action_code = action_code


class ActionConflict(ActionError):
    """The request repeats or races with another (already in that state, stale submission): HTTP 409, nothing changed."""
