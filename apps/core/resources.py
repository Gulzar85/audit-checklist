"""Shared django-import-export helpers."""

from __future__ import annotations

from typing import Any

_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


class FormulaSafeExportMixin:
    """Neutralise spreadsheet formulas in exported text.

    Tenant-entered strings (names, descriptions) end up in exports opened by platform
    staff in Excel/Sheets; a value such as ``=HYPERLINK(...)`` would execute.  Text that
    starts with a formula character is exported with a leading apostrophe, the standard
    mitigation.  (django-import-export 4.4 has no setting for this.)
    """

    def export_field(self, field: Any, instance: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        value = super().export_field(field, instance, **kwargs)  # type: ignore[misc]
        if isinstance(value, str) and value.startswith(_FORMULA_PREFIXES):
            return "'" + value
        return value
