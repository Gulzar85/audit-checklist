"""Human-readable rendering of stored answers (read-only views): option labels, Yes/No, matrix rows, dates."""

from __future__ import annotations

from typing import Any

from apps.checklists.models import ChecklistItem

from .models import ChecklistResponse


def _labels(item: ChecklistItem) -> dict[str, str]:
    return {o.value: o.label for o in item.options.all()}


def format_answer(item: ChecklistItem, response: ChecklistResponse | None) -> dict[str, Any]:
    """``{"kind": "empty"|"na"|"text"|"list"|"matrix", ...}`` for one response; never raw HTML."""
    if response is None:
        return {"kind": "empty"}
    if response.status == "not_applicable":
        return {"kind": "na"}
    value, kind = response.value, item.answer_type
    if kind == "boolean":
        return {"kind": "text", "text": "Yes" if value else "No"}
    if kind in ("yes_no", "yes_no_na", "single_choice"):
        return {"kind": "text", "text": _labels(item).get(value, str(value)) if kind == "single_choice" else {"yes": "Yes", "no": "No", "na": "N/A"}.get(value, str(value))}
    if kind == "multiple_choice":
        labels = _labels(item)
        return {"kind": "list", "items": [labels.get(v, str(v)) for v in value or []]}
    if kind == "matrix" and isinstance(value, dict):
        rows = {r["key"]: r["label"] for r in item.config.get("rows", [])}
        cols = {c["key"]: c["label"] for c in item.config.get("columns", [])}
        def cell(v):  # noqa: ANN001, ANN202
            return ", ".join(cols.get(c, str(c)) for c in v) if isinstance(v, list) else cols.get(v, str(v))
        return {"kind": "matrix", "rows": [(rows.get(k, k), cell(v)) for k, v in value.items()]}
    if kind == "numeric":
        unit = item.config.get("unit", "")
        return {"kind": "text", "text": f"{value} {unit}".strip()}
    if kind == "datetime" and isinstance(value, str):
        return {"kind": "text", "text": value.replace("T", " ")[:16]}
    if kind in ("photo", "file", "signature"):
        return {"kind": "text", "text": "(attachment)"}
    return {"kind": "text", "text": "" if value is None else str(value)}
