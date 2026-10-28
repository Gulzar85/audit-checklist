"""Errors of the reporting subsystem."""

from __future__ import annotations


class ReportError(Exception):
    def __init__(self, message: str, code: str = "report_error", *, extra: dict | None = None) -> None:
        super().__init__(message)
        self.message, self.code, self.extra = message, code, extra or {}


class ReportConflict(ReportError):
    """The action is not allowed in the current state (HTTP 409)."""
