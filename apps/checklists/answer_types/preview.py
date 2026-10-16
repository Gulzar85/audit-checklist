"""Design-time preview of an answer type (what the builder shows beside the item editor).

This is a *configuration preview*, not the execution UI (Phase 9): it renders disabled inputs from the type's
normalized configuration so an author can see the effect of a range, unit or option list before saving.
"""

from __future__ import annotations

from typing import Any

from .base import AnswerTypeDefinition

_SAMPLE_OPTIONS = ("Option 1", "Option 2", "Option 3")
_MAX_SCORE_BUTTONS = 11


def preview_context(
    definition: AnswerTypeDefinition, config: dict[str, Any], *, question: str = "", options: list[str] | None = None
) -> dict[str, Any]:
    context: dict[str, Any] = {
        "kind": definition.preview, "definition": definition, "config": config, "question": question,
        "options": list(options) if options else list(_SAMPLE_OPTIONS),
        "sample_options": not options,
    }
    if definition.code == "score":
        values = list(range(config["min"], config["max"] + 1, config["step"]))
        context["score_values"] = values
        context["score_too_many"] = len(values) > _MAX_SCORE_BUTTONS
        context["stars"] = list(range(1, min(len(values), 10) + 1))
    if definition.code in ("photo", "file"):
        context["extensions"] = ", ".join(e.upper() for e in config.get("allowed_extensions", []))
    return context
