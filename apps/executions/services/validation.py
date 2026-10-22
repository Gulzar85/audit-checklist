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

EVIDENCE_VIA_UPLOAD = "Photos, files and signatures are added with the evidence upload, not typed as an answer."


def is_media(answer_type: str) -> bool:
    return answer_type in registry and registry.get(answer_type).capabilities.is_media


def problem(code: str, message: str) -> dict[str, str]:
    return {"code": code, "message": message}


def _evidence_shortfall(state: ExecutionState, spec: Any, kinds: tuple[str, ...], need: int) -> str | None:
    """Why the item has too little *valid* evidence (only AVAILABLE, server-verified files count), or ``None`` when it has enough."""
    attached = state.evidence.get(spec.id, [])
    ready = [a for a in attached if a.asset.status == "available" and (not kinds or a.asset.kind in kinds)]
    if len(ready) >= need:
        return None
    waiting = [a for a in attached if a.asset.status == "pending_scan"]
    blocked = [a for a in attached if a.asset.status == "quarantined"]
    label = " or ".join(kinds) if kinds else "file"
    text = f"Add at least {need} {label}{'s' if need != 1 else ''} (verified evidence)."
    if waiting:
        text += f" {len(waiting)} file(s) are still waiting for the security check."
    if blocked:
        text += f" {len(blocked)} file(s) were quarantined and do not count."
    return text


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
            problems.append(problem("required", (_evidence_shortfall(state, spec, (spec.answer_type,), 1) or "Evidence is required.") if is_media(spec.answer_type)
                                    else "An answer is required."))
        if (result.comment_required and (status in (ANSWERED, NOT_APPLICABLE) or result.required)
                and not (response and response.comment.strip())):
            problems.append(problem("comment_required", (result.comment_prompts[0] if result.comment_prompts else "A comment is required.")))
        if result.evidence:
            shortfall = _evidence_shortfall(state, spec, tuple(result.evidence.get("kinds", ())), int(result.evidence.get("min_count", 1)))
            if shortfall:
                problems.append(problem("evidence_required", shortfall))
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
