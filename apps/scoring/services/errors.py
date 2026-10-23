"""Errors of the scoring services (mapped to 400 / 409 by the API and to messages by the views)."""

from __future__ import annotations


class ScoringError(Exception):
    """A refused scoring operation with a stable ``code`` and a user-readable ``message``."""

    status = 400

    def __init__(self, message: str, code: str = "scoring_error", *, extra: dict | None = None) -> None:
        super().__init__(message)
        self.message, self.code, self.extra = message, code, extra or {}


class ScoringConflict(ScoringError):
    status = 409
