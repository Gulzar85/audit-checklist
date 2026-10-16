"""Errors raised by the answer-type engine.

Two different failures, never mixed:

* :class:`ConfigurationError` - an item's *definition* is malformed (author error, caught when the item is saved);
* :class:`AnswerInvalid` - a *submitted answer* does not satisfy the definition (user error, caught at answer time).
"""

from __future__ import annotations


class ConfigurationError(Exception):
    """Raised by ``clean_config``; ``messages`` are human-readable, ``key`` names the offending setting."""

    def __init__(self, message: str, key: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.key = key

    @property
    def messages(self) -> list[str]:
        return [self.message]


class AnswerInvalid(Exception):
    """Raised inside a definition's ``normalize``; carries a stable machine ``code`` and a message."""

    def __init__(self, code: str, message: str, path: str = "") -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.path = path
