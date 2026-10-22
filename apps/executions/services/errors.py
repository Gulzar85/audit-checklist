"""Errors of the execution services.  Django ``ValidationError`` subclasses, so every channel can show them."""

from __future__ import annotations

from typing import Any

from django.core.exceptions import ValidationError


class ExecutionError(ValidationError):
    """A business rule refused the operation; ``execution_code`` says which (``not_editable``, ``not_executable`` ...)."""

    def __init__(self, message: Any, execution_code: str = "execution_error") -> None:
        super().__init__(message, code=execution_code)
        self.execution_code = execution_code


class ExecutionConflict(ExecutionError):
    """The request repeats or races with another (already submitted, stale revision, duplicate start): HTTP 409."""


class ExecutionValidationError(ExecutionError):
    """Answers failed validation; ``problems`` maps an item id to a list of ``{"code", "message"}`` dicts (``""`` = whole checklist)."""

    def __init__(self, problems: dict[str, list[dict[str, str]]], message: str = "The checklist has validation problems.") -> None:
        super().__init__(message, "validation_failed")
        self.problems = problems
