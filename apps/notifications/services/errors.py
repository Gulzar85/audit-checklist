"""Errors of the notification services (mapped to 400 / 409 by the API and to messages by the views)."""

from __future__ import annotations


class NotificationError(Exception):
    status = 400

    def __init__(self, message: str, code: str = "notification_error", *, extra: dict | None = None) -> None:
        super().__init__(message)
        self.message, self.code, self.extra = message, code, extra or {}


class NotificationConflict(NotificationError):
    status = 409
