"""Centralised API exception handling.

Turns every DRF/Django exception into the standard error envelope while
preserving the correct HTTP status code.
"""

from __future__ import annotations

from typing import Any

from rest_framework import exceptions as drf_exceptions
from rest_framework.response import Response
from rest_framework.views import exception_handler as drf_exception_handler

from .response import ErrorCode

_STATUS_TO_CODE = {
    400: ErrorCode.VALIDATION_ERROR,
    401: ErrorCode.NOT_AUTHENTICATED,
    403: ErrorCode.PERMISSION_DENIED,
    404: ErrorCode.NOT_FOUND,
    405: ErrorCode.METHOD_NOT_ALLOWED,
    409: ErrorCode.CONFLICT,
    429: ErrorCode.THROTTLED,
}


def api_exception_handler(exc: Exception, context: dict[str, Any]) -> Response | None:
    """Return a response containing the standard error envelope."""
    response = drf_exception_handler(exc, context)
    if response is None:
        return None

    code = _STATUS_TO_CODE.get(response.status_code, ErrorCode.INTERNAL_ERROR)
    if isinstance(exc, drf_exceptions.ValidationError):
        code = ErrorCode.VALIDATION_ERROR

    message = _message_for(exc)
    details = response.data if isinstance(response.data, (dict, list)) else {}

    response.data = {
        "error": {
            "code": str(code),
            "message": message,
            "details": details,
        }
    }
    return response


def _message_for(exc: Exception) -> str:
    detail = getattr(exc, "detail", None)
    if detail is not None:
        if isinstance(detail, (list, tuple)) and detail:
            return str(detail[0])
        return str(detail)
    return "An unexpected error occurred."
