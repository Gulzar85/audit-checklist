"""Standardised API response contract.

**Success**

    {
        "data": <payload>,
        "meta": {"count": 12, "next": "...", "previous": "..."}
    }

**Error**

    {
        "error": {
            "code": "VALIDATION_ERROR",
            "message": "Human-readable summary.",
            "details": {...}
        }
    }

The envelope is applied centrally by
:class:`apps.api.renderers.EnvelopeJSONRenderer` and
:func:`apps.api.exceptions.api_exception_handler`; individual views return plain
payloads.
"""

from __future__ import annotations

from enum import StrEnum


class ErrorCode(StrEnum):
    """Stable, machine-readable error codes."""

    VALIDATION_ERROR = "VALIDATION_ERROR"
    NOT_FOUND = "NOT_FOUND"
    PERMISSION_DENIED = "PERMISSION_DENIED"
    NOT_AUTHENTICATED = "NOT_AUTHENTICATED"
    THROTTLED = "THROTTLED"
    METHOD_NOT_ALLOWED = "METHOD_NOT_ALLOWED"
    CONFLICT = "CONFLICT"
    INTERNAL_ERROR = "INTERNAL_ERROR"
