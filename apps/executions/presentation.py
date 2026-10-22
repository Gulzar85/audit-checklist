"""View-models for the execution workspace: the version's sections and items joined with the server's evaluation.

The template never decides anything: visibility, requiredness, available options and constraints come from the Phase 6
evaluation of the stored answers (the server is the source of truth); this module only arranges them for rendering.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from apps.checklists.answer_types import registry
from apps.checklists.rules.state import ANSWERED, NOT_APPLICABLE

from .services.evaluation import ExecutionState
from .services.validation import is_media


@dataclass
class ItemView:
    item: Any
    result: Any
    response: Any
    widget: str
    required: bool
    options: list[dict[str, Any]] = field(default_factory=list)
    value: Any = ""
    not_applicable: bool = False
    comment: str = ""
    revision: int = 0
    attrs: dict[str, Any] = field(default_factory=dict)
    rows: list[dict[str, Any]] = field(default_factory=list)
    multiple: bool = False
    errors: list[str] = field(default_factory=list)
    conflict: bool = False
    can_na: bool = False

    @property
    def id(self) -> str:
        return str(self.item.pk)

    @property
    def answered(self) -> bool:
        return self.result.answer.status in (ANSWERED, NOT_APPLICABLE)


@dataclass
class SectionView:
    section: Any
    visible: bool
    items: list[ItemView]
    answered: int = 0
    total: int = 0

    @property
    def code(self) -> str:
        return self.section.code


def widget_for(item: Any) -> str:
    kind = item.answer_type
    if kind in ("yes_no", "yes_no_na"):
        return "radio"
    if kind == "single_choice":
        return "select" if item.config.get("display") == "dropdown" else "radio"
    if kind in registry.codes() and is_media(kind):
        return "media"
    return {"boolean": "boolean", "multiple_choice": "checkbox", "numeric": "number", "score": "score", "text": "text", "long_text": "textarea",
            "date": "date", "datetime": "datetime", "matrix": "matrix"}.get(kind, "unsupported")


def _options(item: Any, result: Any) -> list[dict[str, Any]]:
    available = result.options["available"] if result.options else None
    return [{"value": o.value, "label": o.label} for o in item.options.all()
            if o.is_active and (available is None or o.value in available)]


def build_sections(state: ExecutionState, sections: Any, *, posted: dict[str, Any] | None = None, errors: dict[str, list[str]] | None = None,
                   conflicts: set[str] | None = None) -> list[SectionView]:
    """``posted`` = raw values (by item id) to show instead of the stored ones (answers that were refused)."""
    posted, errors, conflicts = posted or {}, errors or {}, conflicts or set()
    ev = state.evaluation
    out: list[SectionView] = []
    for section in sections:
        sec_visible = ev.sections.get(section.code, {}).get("visible", True)
        views: list[ItemView] = []
        for item in section.items.all():
            result = ev.items.get(item.code)
            if result is None or not result.visible:
                continue
            response = state.responses.get(item.code)
            widget = widget_for(item)
            iv = ItemView(item=item, result=result, response=response, widget=widget, required=result.required,
                          revision=response.revision if response else 0, comment=response.comment if response else "",
                          not_applicable=bool(response and response.status == "not_applicable"),
                          can_na=not result.required and widget not in ("media",), errors=errors.get(str(item.pk), []),
                          conflict=str(item.pk) in conflicts)
            value = posted.get(str(item.pk), response.value if response and response.status == "answered" else None)
            config = {**item.config, **(result.constraints or {})}
            if widget in ("radio", "select", "checkbox"):
                iv.options = _options(item, result)
                chosen = set(value) if isinstance(value, (list, tuple)) else {value}
                for o in iv.options:
                    o["checked"] = o["value"] in chosen
                iv.value = value
                iv.multiple = widget == "checkbox"
            elif widget == "boolean":
                iv.value = "true" if value in (True, "true") else "false" if value in (False, "false") else ""
            elif widget == "number":
                decimals = config.get("decimals", 2)
                iv.attrs = {"step": "1" if decimals == 0 else "0." + "0" * (decimals - 1) + "1", "min": config.get("min"), "max": config.get("max"),
                            "unit": config.get("unit", "")}
                iv.value = "" if value is None else value
            elif widget == "score":
                iv.options = [{"value": str(v), "label": str(v), "checked": value is not None and str(value) == str(v)}
                              for v in range(config["min"], config["max"] + 1, config["step"])]
                iv.attrs = {"stars": config.get("display") == "stars"}
                iv.value = value
            elif widget in ("text", "textarea"):
                iv.attrs = {"maxlength": config.get("max_length", 255)}
                iv.value = "" if value is None else value
            elif widget == "datetime":
                iv.value = (value or "")[:16] if isinstance(value, str) else ""
            elif widget == "date":
                iv.value = value or ""
            elif widget == "matrix":
                iv.multiple = config.get("mode", "single") == "multiple"
                cur = value if isinstance(value, dict) else {}
                iv.rows = [{"key": r["key"], "label": r["label"], "cells": [
                    {"key": c["key"], "label": c["label"],
                     "checked": (c["key"] in (cur.get(r["key"]) or [])) if iv.multiple else cur.get(r["key"]) == c["key"]}
                    for c in config["columns"]]} for r in config["rows"]]
            views.append(iv)
        total = sum(1 for v in views if v.required)
        answered = sum(1 for v in views if v.required and v.answered)
        out.append(SectionView(section, sec_visible and bool(views), views, answered, total))
    return out
