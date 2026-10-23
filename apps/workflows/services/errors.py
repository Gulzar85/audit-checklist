"""Errors of the workflow services.  Django ``ValidationError`` subclasses, so every channel (web, API, commands) can show them."""

from __future__ import annotations

from typing import Any

from django.core.exceptions import ValidationError


class WorkflowError(ValidationError):
    """A rule refused the operation; ``workflow_code`` says which (``invalid_transition``, ``no_branch``, ``no_assignee`` ...)."""

    def __init__(self, message: Any, workflow_code: str = "workflow_error") -> None:
        super().__init__(message, code=workflow_code)
        self.workflow_code = workflow_code


class WorkflowConflict(WorkflowError):
    """The request repeats or races with another (stale revision, already decided, not active): HTTP 409, nothing changed."""


class WorkflowValidationError(WorkflowError):
    """A draft version failed publication validation; ``report`` carries the errors and warnings."""

    def __init__(self, report: Any) -> None:
        super().__init__("; ".join(report.errors) or "The workflow is not valid.", "validation_failed")
        self.report = report
