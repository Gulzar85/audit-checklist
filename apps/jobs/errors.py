"""Errors of the scheduling subsystem.  ``RetryableError`` / ``PermanentError`` are what a task handler raises to say whether trying again can help."""

from __future__ import annotations


class JobError(Exception):
    def __init__(self, message: str, code: str = "job_error", *, extra: dict | None = None) -> None:
        super().__init__(message)
        self.message, self.code, self.extra = message, code, extra or {}


class JobConflict(JobError):
    """The job / schedule is not in a state that allows the action (HTTP 409)."""


class RetryableError(Exception):
    """A temporary failure (a provider, the network, a lock): the runner schedules another attempt within the task's bounded retry policy.  The message is stored sanitized."""


class PermanentError(Exception):
    """A failure no retry can fix (bad configuration, ineligible record): the job fails at once."""
