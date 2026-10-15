"""Tiny, dependency-free helpers shared across apps."""

from __future__ import annotations

import uuid
from typing import Any

from django.http import HttpRequest


def parse_uuid(value: Any) -> uuid.UUID | None:
    """Return ``value`` as a ``UUID``, or ``None`` if it is not a valid one.

    Query-string values are user input; feeding a malformed one to the ORM would
    raise instead of simply matching nothing.
    """
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (ValueError, AttributeError, TypeError):
        return None


def is_htmx(request: HttpRequest) -> bool:
    """A normal HTMX request (not a history restore, which needs the full page)."""
    return (
        request.headers.get("HX-Request") == "true"
        and request.headers.get("HX-History-Restore-Request") != "true"
    )
