"""The action registry: what a matching rule may do, to what, and with which parameters.

Actions are declarative effects on a *target* (an item, a section, or the checklist as a whole for a message).  There is
no action that runs code, queries data or changes stored answers - the engine only computes an effective state that the
caller (web, API, later execution) then enforces.

=======================  ==========  ==============================================================  ============
action                   target      effect                                                          graph edge
=======================  ==========  ==============================================================  ============
SHOW_ITEM / HIDE_ITEM    item        visibility of the item                                          yes
SHOW_SECTION / HIDE_...  section     visibility of the section (and so of its items)                 yes
REQUIRE_ITEM             item        the item must be answered (when visible)                        no
MAKE_OPTIONAL            item        the item need not be answered                                   no
DISPLAY_MESSAGE          item/section/none  an info / warning / error message                       no
REQUIRE_COMMENT          item        a comment is required with the answer                           no
REQUIRE_EVIDENCE         item        evidence (photo / file / signature) is required                 no
SET_VALIDATION_CONSTRAINT item       tighten the answer-type limits (min/max ...)                    yes
OPTION_AVAILABILITY      item        restrict which existing options may be chosen                   yes
ADD_INSTRUCTIONS         item        extra instruction text for the user                             no
RAISE_FINDING            item        a finding the server records on submission (Phase 12)           no
=======================  ==========  ==============================================================  ============

"Graph edge" actions change whether another rule's *source answer* counts (hidden / out-of-option / constraint-invalid
answers are ignored), so they take part in cycle detection and evaluation order; the others only annotate the result.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from .constraints import ConstraintError, clean_constraints
from .errors import Issue
from .limits import limit
from .snapshot import ItemSpec, VersionSnapshot

_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
SEVERITIES = ("info", "warning", "error")
EVIDENCE_KINDS = ("photo", "file", "signature")

#: Which effective property an action changes (used for conflict handling and traces).
VISIBILITY, REQUIREDNESS, OPTIONS, CONSTRAINTS, COMMENT, EVIDENCE, MESSAGE, INSTRUCTIONS, FINDING = (
    "visibility", "requiredness", "options", "constraints", "comment", "evidence", "message", "instructions", "finding",
)


class ActionError(Exception):
    def __init__(self, message: str, path: str = "") -> None:
        super().__init__(message)
        self.message, self.path = message, path


@dataclass(frozen=True)
class ActionDef:
    code: str
    label: str
    description: str
    #: ``item`` | ``section`` | ``optional`` (item, section or none - messages only)
    target: str
    prop: str
    edge: bool
    clean_params: Callable[[ItemSpec | None, dict[str, Any]], dict[str, Any]]
    #: Parameter documentation for the API / rule builder: ``[{key, kind, required, choices?, help}]``.
    params: tuple[dict[str, Any], ...] = ()

    def describe(self) -> dict[str, Any]:
        return {"code": self.code, "label": self.label, "description": self.description, "target": self.target,
                "property": self.prop, "affects_evaluation_order": self.edge, "params": [dict(p) for p in self.params]}


def _text(raw: Any, key: str, maximum: int, *, required: bool = True) -> str:
    if raw in (None, ""):
        if required:
            raise ActionError(f"“{key}” is required.")
        return ""
    if not isinstance(raw, str) or _CONTROL.search(raw):
        raise ActionError(f"“{key}” must be plain text.")
    text = raw.strip()
    if required and not text:
        raise ActionError(f"“{key}” is required.")
    if len(text) > maximum:
        raise ActionError(f"“{key}” may be at most {maximum} characters.")
    return text


def _no_params(item: ItemSpec | None, params: dict[str, Any]) -> dict[str, Any]:
    if params:
        raise ActionError("This action takes no parameters: " + ", ".join(sorted(params)) + ".")
    return {}


def _only(params: dict[str, Any], allowed: tuple[str, ...]) -> None:
    unknown = sorted(set(params) - set(allowed))
    if unknown:
        raise ActionError("Unexpected parameters: " + ", ".join(unknown) + ". Allowed: " + (", ".join(allowed) or "none") + ".")


def _message(item: ItemSpec | None, params: dict[str, Any]) -> dict[str, Any]:
    _only(params, ("severity", "message"))
    severity = params.get("severity", "info")
    if severity not in SEVERITIES:
        raise ActionError("“severity” must be one of: " + ", ".join(SEVERITIES) + ".")
    return {"severity": severity, "message": _text(params.get("message"), "message", limit("max_message_length"))}


def _comment(item: ItemSpec | None, params: dict[str, Any]) -> dict[str, Any]:
    _only(params, ("prompt",))
    prompt = _text(params.get("prompt"), "prompt", 200, required=False)
    return {"prompt": prompt} if prompt else {}


def _evidence(item: ItemSpec | None, params: dict[str, Any]) -> dict[str, Any]:
    _only(params, ("kinds", "min_count"))
    kinds = params.get("kinds", ["photo"])
    if not isinstance(kinds, (list, tuple)) or not kinds or not all(k in EVIDENCE_KINDS for k in kinds) or len(set(kinds)) != len(kinds):
        raise ActionError("“kinds” must be a non-empty list from: " + ", ".join(EVIDENCE_KINDS) + ".")
    count = params.get("min_count", 1)
    if isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= 10:
        raise ActionError("“min_count” must be a whole number from 1 to 10.")
    return {"kinds": list(kinds), "min_count": count}


def _constraint(item: ItemSpec | None, params: dict[str, Any]) -> dict[str, Any]:
    _only(params, ("constraints",))
    try:
        return {"constraints": clean_constraints(item, params.get("constraints"))}  # type: ignore[arg-type]
    except ConstraintError as exc:
        raise ActionError(str(exc), "constraints") from None


def _options(item: ItemSpec | None, params: dict[str, Any]) -> dict[str, Any]:
    _only(params, ("mode", "options"))
    assert item is not None
    definition = item.definition
    if definition.comparison_family not in ("choice", "multi_choice") or definition.capabilities.has_fixed_options:
        raise ActionError("Option availability applies to single-choice and multiple-choice questions only.")
    mode = params.get("mode")
    if mode not in ("only", "exclude"):
        raise ActionError("“mode” must be “only” (these options) or “exclude” (all but these).")
    values = params.get("options")
    if not isinstance(values, (list, tuple)) or not values:
        raise ActionError("“options” must list at least one option.", "options")
    if len(values) > limit("max_operand_items") or len(set(values)) != len(values):
        raise ActionError("“options” has too many or duplicate entries.", "options")
    known = item.option_values()
    missing = [v for v in values if v not in known]
    if missing:
        raise ActionError("Unknown option(s) on the question: " + ", ".join(str(m) for m in missing[:5]) + ".", "options")
    active = [o.value for o in item.options if o.is_active]
    remaining = [v for v in active if (v in values) == (mode == "only")]
    if not remaining:
        raise ActionError("This would leave no option available.", "options")
    return {"mode": mode, "options": list(values)}


#: Placeholders a finding title / description may use.  They are filled by plain substitution (never ``str.format``/``eval``).
FINDING_TOKENS = ("question", "answer", "location", "checklist")
_CODE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")
_TOKEN = re.compile(r"\{([^{}]*)\}")


def _finding_text(raw: Any, key: str, maximum: int) -> str:
    text = _text(raw, key, maximum, required=False)
    for token in _TOKEN.findall(text):
        if token not in FINDING_TOKENS:
            raise ActionError(f"“{key}” may only use the placeholders: " + ", ".join("{" + t + "}" for t in FINDING_TOKENS) + ".")
    if text.count("{") != text.count("}"):
        raise ActionError(f"“{key}” has an unbalanced brace.")
    return text


def _finding(item: ItemSpec | None, params: dict[str, Any]) -> dict[str, Any]:
    _only(params, ("category", "severity", "title", "description", "requires_review", "inherit_evidence"))
    cleaned: dict[str, Any] = {}
    for key in ("category", "severity"):
        value = params.get(key)
        if not isinstance(value, str) or not _CODE.match(value.strip().lower()):
            raise ActionError(f"“{key}” must be the code of an organization {key} (lowercase letters, digits, - and _).")
        cleaned[key] = value.strip().lower()
    title = _finding_text(params.get("title"), "title", 200)
    description = _finding_text(params.get("description"), "description", 1000)
    if title:
        cleaned["title"] = title
    if description:
        cleaned["description"] = description
    for key, default in (("requires_review", False), ("inherit_evidence", True)):
        value = params.get(key, default)
        if not isinstance(value, bool):
            raise ActionError(f"“{key}” must be true or false.")
        cleaned[key] = value
    return cleaned


def _instructions(item: ItemSpec | None, params: dict[str, Any]) -> dict[str, Any]:
    _only(params, ("text",))
    return {"text": _text(params.get("text"), "text", limit("max_instruction_length"))}


ACTIONS: dict[str, ActionDef] = {
    a.code: a
    for a in (
        ActionDef("SHOW_ITEM", "Show question", "Show the question when the condition matches (hidden otherwise).", "item", VISIBILITY, True, _no_params),
        ActionDef("HIDE_ITEM", "Hide question", "Hide the question when the condition matches.", "item", VISIBILITY, True, _no_params),
        ActionDef("SHOW_SECTION", "Show section", "Show the section when the condition matches (hidden otherwise).", "section", VISIBILITY, True, _no_params),
        ActionDef("HIDE_SECTION", "Hide section", "Hide the section when the condition matches.", "section", VISIBILITY, True, _no_params),
        ActionDef("REQUIRE_ITEM", "Make required", "The question must be answered (while visible).", "item", REQUIREDNESS, False, _no_params),
        ActionDef("MAKE_OPTIONAL", "Make optional", "The question need not be answered.", "item", REQUIREDNESS, False, _no_params),
        ActionDef("DISPLAY_MESSAGE", "Display a message", "Show an information, warning or error message.", "optional", MESSAGE, False, _message,
                  ({"key": "severity", "kind": "choice", "choices": list(SEVERITIES), "required": False}, {"key": "message", "kind": "text", "required": True})),
        ActionDef("REQUIRE_COMMENT", "Require a comment", "A comment must accompany the answer.", "item", COMMENT, False, _comment,
                  ({"key": "prompt", "kind": "text", "required": False},)),
        ActionDef("REQUIRE_EVIDENCE", "Require evidence", "Evidence (photo, file or signature) must accompany the answer; stored by the evidence phase.", "item", EVIDENCE, False, _evidence,
                  ({"key": "kinds", "kind": "list", "choices": list(EVIDENCE_KINDS), "required": False}, {"key": "min_count", "kind": "int", "required": False})),
        ActionDef("SET_VALIDATION_CONSTRAINT", "Tighten validation", "Narrow the answer-type limits (never widen them).", "item", CONSTRAINTS, True, _constraint,
                  ({"key": "constraints", "kind": "object", "required": True},)),
        ActionDef("OPTION_AVAILABILITY", "Restrict options", "Make only some of the question's options available.", "item", OPTIONS, True, _options,
                  ({"key": "mode", "kind": "choice", "choices": ["only", "exclude"], "required": True}, {"key": "options", "kind": "list", "required": True})),
        ActionDef("RAISE_FINDING", "Raise a finding", "When the question is submitted with a matching answer, record a finding (idempotent; created by the server on submission).",
                  "item", FINDING, False, _finding,
                  ({"key": "category", "kind": "text", "required": True}, {"key": "severity", "kind": "text", "required": True},
                   {"key": "title", "kind": "text", "required": False}, {"key": "description", "kind": "text", "required": False},
                   {"key": "requires_review", "kind": "bool", "required": False}, {"key": "inherit_evidence", "kind": "bool", "required": False})),
        ActionDef("ADD_INSTRUCTIONS", "Add instructions", "Show extra instructions for the question.", "item", INSTRUCTIONS, False, _instructions,
                  ({"key": "text", "kind": "text", "required": True},)),
    )
}


def describe_all() -> list[dict[str, Any]]:
    return [a.describe() for a in ACTIONS.values()]


def clean_action(snapshot: VersionSnapshot, raw: Any, path: str) -> tuple[dict[str, Any], tuple[str, str, str, dict[str, Any]] | None, list[Issue]]:
    """Validate one action object.  Returns ``(normalized, (type, kind, code, params) | None, issues)``."""
    issues: list[Issue] = []

    def fail(message: str, sub: str = "") -> None:
        issues.append(Issue("error", "invalid_action", message, path=f"{path}{sub}"))

    if not isinstance(raw, dict):
        fail("An action must be an object.")
        return {}, None, issues
    body = dict(raw)
    action_type = str(body.pop("type", "")).strip().upper()
    definition = ACTIONS.get(action_type)
    if definition is None:
        fail(f"Unknown action “{action_type}”.", ".type")
        return {}, None, issues
    target = body.pop("target", None)
    kind = code = ""
    item: ItemSpec | None = None
    if target is None:
        if definition.target != "optional":
            fail(f"{definition.label} needs a target.", ".target")
    elif not isinstance(target, dict) or set(target) - {"type", "code"} or not target.get("code"):
        fail("A target is {\"type\": \"item\"|\"section\", \"code\": \"...\"}.", ".target")
    else:
        kind, code = str(target.get("type", "")).lower(), str(target["code"])
        wanted = ("item", "section") if definition.target == "optional" else (definition.target,)
        if kind not in wanted:
            fail(f"{definition.label} targets a {' or '.join(wanted)}, not a {kind or 'target of unknown type'}.", ".target.type")
            kind = ""
        elif kind == "item" and code not in snapshot.items:
            fail(f"Unknown question “{code}” in this checklist version.", ".target.code")
            kind = ""
        elif kind == "section" and code not in snapshot.sections:
            fail(f"Unknown section “{code}” in this checklist version.", ".target.code")
            kind = ""
        elif kind == "item":
            item = snapshot.items[code]
    if issues:
        return {}, None, issues
    try:
        params = definition.clean_params(item, body)
    except ActionError as exc:
        fail(exc.message, f".{exc.path}" if exc.path else "")
        return {}, None, issues
    normalized: dict[str, Any] = {"type": action_type, **params}
    if kind:
        normalized["target"] = {"type": kind, "code": code}
    return normalized, (action_type, kind, code, params), issues
