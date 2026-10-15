"""Small, shared field validators.

These are format validators only: they never touch the database.  Cross-field
and cross-row business rules live on the models and in the service layer.
"""

from __future__ import annotations

import re
from functools import lru_cache
from zoneinfo import available_timezones

from django.core.exceptions import ValidationError
from django.utils.translation import gettext_lazy as _

COUNTRY_CODE_RE = re.compile(r"^[A-Z]{2}$")
CURRENCY_CODE_RE = re.compile(r"^[A-Z]{3}$")
LANGUAGE_CODE_RE = re.compile(r"^[a-z]{2,3}(-[A-Za-z0-9]{2,8})*$")
HEX_COLOR_RE = re.compile(r"^#[0-9A-Fa-f]{6}$")


@lru_cache(maxsize=1)
def _timezone_names() -> frozenset[str]:
    return frozenset(available_timezones())


def validate_timezone(value: str) -> None:
    """Accept only IANA time zone names (``Europe/London``, ``UTC`` ...)."""
    if value and value not in _timezone_names():
        raise ValidationError(
            _("“%(value)s” is not a recognised IANA time zone."), params={"value": value}
        )


def validate_country_code(value: str) -> None:
    """Accept an upper-case ISO 3166-1 alpha-2 code (``GB``)."""
    if value and not COUNTRY_CODE_RE.match(value):
        raise ValidationError(_("Enter a two-letter ISO country code, e.g. “GB”."))


def validate_currency_code(value: str) -> None:
    """Accept an upper-case ISO 4217 code (``USD``)."""
    if value and not CURRENCY_CODE_RE.match(value):
        raise ValidationError(_("Enter a three-letter ISO currency code, e.g. “USD”."))


def validate_language_code(value: str) -> None:
    """Accept a BCP 47-style language tag (``en``, ``en-gb``)."""
    if value and not LANGUAGE_CODE_RE.match(value):
        raise ValidationError(_("Enter a language code such as “en” or “en-gb”."))


def validate_hex_color(value: str) -> None:
    """Accept ``#RRGGBB``."""
    if value and not HEX_COLOR_RE.match(value):
        raise ValidationError(_("Enter a colour as #RRGGBB, e.g. “#4F46E5”."))
