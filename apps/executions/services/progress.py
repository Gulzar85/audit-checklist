"""Operational progress of an execution (not a score).

Denominator: the **visible, required** items (hidden conditional questions never count as unanswered).  An execution with no
visible required item falls back to the visible items; with none at all progress is 0 (nothing to do is not "done").
Numerator: those items with a valid answer (an answered value, or a valid N/A).  Always computed on the server from the saved
responses - never from client-supplied counts - and shared by the UI, the API and the worklist.
"""

from __future__ import annotations

from typing import Any

from apps.checklists.rules.state import ANSWERED, NOT_APPLICABLE

from .evaluation import ExecutionState


def compute_progress(state: ExecutionState) -> dict[str, Any]:
    items = state.evaluation.items
    visible = [r for r in items.values() if r.visible]
    required = [r for r in visible if r.required]
    basis = required or visible
    done = [r for r in basis if r.answer.status in (ANSWERED, NOT_APPLICABLE)]
    percent = round(100 * len(done) / len(basis)) if basis else 0
    return {
        "percent": percent,
        "basis": "required" if required else "visible",
        "total": len(basis),
        "answered": len(done),
        "visible_items": len(visible),
        "required_items": len(required),
        "answered_required": sum(1 for r in required if r.answer.status in (ANSWERED, NOT_APPLICABLE)),
        "unanswered_required": [state.snapshot.items[r.code].id for r in required if r.answer.status not in (ANSWERED, NOT_APPLICABLE)],
        "sections": {
            code: {
                "total": sum(1 for r in basis if r.section == code),
                "answered": sum(1 for r in done if r.section == code),
            } for code in state.snapshot.sections
        },
    }


def progress_for_executions(executions: list) -> dict[Any, int]:
    """``{execution pk: percent}`` for many executions with a constant number of queries per version (worklists)."""
    from collections import defaultdict

    from apps.checklists.rules.engine import evaluate

    from ..models import ChecklistResponse
    from .evaluation import answers_from, compile_for

    by_version = defaultdict(list)
    for ex in executions:
        by_version[ex.version_id].append(ex)
    responses = defaultdict(dict)
    for r in ChecklistResponse.objects.filter(execution__in=executions).select_related("item"):
        responses[r.execution_id][r.item.code] = r
    out: dict[Any, int] = {}
    for group in by_version.values():
        snapshot, compiled = compile_for(group[0])
        for ex in group:
            answers, na = answers_from(responses[ex.pk])
            state = ExecutionState(ex, snapshot, compiled, responses[ex.pk], evaluate(compiled, answers, na))
            out[ex.pk] = compute_progress(state)["percent"]
    return out
