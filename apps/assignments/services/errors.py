"""Errors of the assignment services.  They are Django ``ValidationError`` subclasses so every channel can show them."""

from __future__ import annotations

from typing import Any

from django.core.exceptions import ValidationError


class AssignmentError(ValidationError):
    """A business rule refused the operation; ``assignment_code`` says which (``invalid_transition``, ``duplicate`` ...)."""

    def __init__(self, message: Any, assignment_code: str = "assignment_error") -> None:
        super().__init__(message, code=assignment_code)
        self.assignment_code = assignment_code


class AssignmentConflict(AssignmentError):
    """The request repeats or races with another (already issued / cancelled, duplicate open assignment): HTTP 409."""
