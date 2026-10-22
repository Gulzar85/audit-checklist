"""Response validation (``ResponseValidationService``): the answer rules shared by the HTML forms and the REST API.

There is no validation logic anywhere else - not in templates, serializers or JavaScript.  The Phase 5 answer-type validators
run inside the Phase 6 evaluation, so an answer is judged against the *exact version's* item configuration **as tightened by
the rules currently in effect** (option availability, constraints).  Hidden or inactive items accept no answers.
"""

from __future__ import annotations

from typing import Any

from apps.checklists.answer_types import registry
from apps.checklists.rules.state import ANSWERED, EMPTY, INVALID, NOT_APPLICABLE

from .evaluation import ExecutionState

EVIDENCE_UNAVAILABLE = "Photos, files and signatures cannot be captured yet (secure evidence storage arrives in a later release)."


def is_media(answer_type: str) -> bool:
    return answer_type in registry and registry.get(answer_type).capabilities.is_media


def problem(code: str, message: str) -> dict[str, str]:
    return {"code": code, "message": message}


def submission_problems(state: ExecutionState) -> dict[str, list[dict[str, str]]]:
    """Everything that blocks submission, per item id (``""`` = the checklist as a whole).  Empty means it may be submitted."""
    found: dict[str, list[dict[str, str]]] = {}
    ev = state.evaluation
    if any(e.get("code") == "cycle" for e in ev.errors):
        found.setdefault("", []).append(problem("rules_invalid", "This checklist's rules cannot be evaluated; it cannot be submitted."))
    for code, result in ev.items.items():
        if not result.visible:
            continue
        spec = state.snapshot.items[code]
        problems: list[dict[str, str]] = []
        status = result.answer.status
        response = state.responses.get(code)
        if status == INVALID:
            problems += [problem(e.get("code", "invalid"), e.get("message", "This answer is not valid.")) for e in result.answer.errors]
        elif status == EMPTY and result.required:
            problems.append(problem("required", EVIDENCE_UNAVAILABLE if is_media(spec.answer_type) else "An answer is required."))
        if (result.comment_required and (status in (ANSWERED, NOT_APPLICABLE) or result.required)
                and not (response and response.comment.strip())):
            problems.append(problem("comment_required", (result.comment_prompts[0] if result.comment_prompts else "A comment is required.")))
        if result.evidence:
            problems.append(problem("evidence_required", EVIDENCE_UNAVAILABLE))
        if problems:
            found[spec.id] = problems
    return found


def describe(problems: dict[str, list[dict[str, str]]], state: ExecutionState) -> list[dict[str, Any]]:
    """Problems with the question text, for the review page and the API (in checklist order)."""
    out = []
    for spec in sorted(state.snapshot.items.values(), key=lambda i: (state.snapshot.sections[i.section_code].sort_order, i.sort_order)):
        if spec.id in problems:
            out.append({"item": spec.id, "code": spec.code, "section": spec.section_code, "question": spec.question, "problems": problems[spec.id]})
    if "" in problems:
        out.insert(0, {"item": "", "code": "", "section": "", "question": "", "problems": problems[""]})
    return out
