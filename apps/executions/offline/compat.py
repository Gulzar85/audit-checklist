"""What the browser's offline evaluator and answer validators can do - the *compatibility policy* (ADR 0057).

The client evaluator is a deliberately small, versioned subset of the Phase 6 engine.  A checklist that uses anything outside it
is **not offered for offline preparation** (``limited``), never silently degraded: a missing rule could otherwise let an invalid
checklist look complete on the device.  The server re-evaluates everything anyway; this policy only protects the user experience.
Bump ``EVALUATOR_VERSION`` whenever the JavaScript evaluator (``static/js/offline/rules-engine.js``) changes behaviour.
"""

from __future__ import annotations

from typing import Any

from apps.checklists.answer_types import registry
from apps.evidence.validation import describe_limits

SCHEMA_VERSION = 1  # the sync / package contract
EVALUATOR_VERSION = 2  # the JavaScript rules evaluator (2: evidence - REQUIRE_EVIDENCE and photo/file/signature answers)

#: Answer types the offline workspace can render, validate and store.  Evidence types (photo / file / signature) are *answered by uploads*:
#: the device stores the file Blob in IndexedDB and uploads it through the evidence endpoint (Phase 11, docs/offline-evidence-sync.md).
SUPPORTED_ANSWER_TYPES = (
    "boolean", "yes_no", "yes_no_na", "single_choice", "multiple_choice", "score", "numeric", "text", "long_text", "date", "datetime", "matrix",
    "photo", "file", "signature",
)
MEDIA_ANSWER_TYPES = ("photo", "file", "signature")

#: Comparison families the client evaluator implements; text (Unicode case folding) and datetime (time zones) are server-only.
SUPPORTED_FAMILIES = ("equality", "choice", "multi_choice", "ordered")
SUPPORTED_OPERATORS = (
    "EQUALS", "NOT_EQUALS", "IS_EMPTY", "IS_NOT_EMPTY", "GREATER_THAN", "GREATER_THAN_OR_EQUAL", "LESS_THAN", "LESS_THAN_OR_EQUAL",
    "BETWEEN", "NOT_BETWEEN", "SELECTED", "NOT_SELECTED", "CONTAINS_ANY", "CONTAINS_ALL",
)
SUPPORTED_ACTIONS = (
    "SHOW_ITEM", "HIDE_ITEM", "SHOW_SECTION", "HIDE_SECTION", "REQUIRE_ITEM", "MAKE_OPTIONAL", "REQUIRE_COMMENT", "ADD_INSTRUCTIONS",
    "DISPLAY_MESSAGE", "REQUIRE_EVIDENCE",
)

#: Limits of one sync request (abuse control).
MAX_OPERATIONS = 200
MAX_BODY_BYTES = 1_000_000
MAX_VALUE_BYTES = 20_000


def manifest() -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION, "evaluator_version": EVALUATOR_VERSION,
        "supported_answer_types": list(SUPPORTED_ANSWER_TYPES), "unsupported_answer_types": [],
        "supported_operators": list(SUPPORTED_OPERATORS), "supported_actions": list(SUPPORTED_ACTIONS),
        "limits": {"max_operations": MAX_OPERATIONS, "max_body_bytes": MAX_BODY_BYTES, "max_value_bytes": MAX_VALUE_BYTES},
        "operations": ["upsert_response", "clear_response", "submit"],
        "evidence": "supported", "evidence_upload": "/api/v1/evidence/uploads/", "evidence_limits": describe_limits(),
    }


def _condition_problems(node: Any, snapshot_items: dict[str, Any], out: list[str]) -> None:
    if not node:
        return
    if "conditions" in node:
        for child in node["conditions"]:
            _condition_problems(child, snapshot_items, out)
        return
    operator, code = node.get("comparison"), node.get("source_item_code")
    item = snapshot_items.get(code)
    if operator not in SUPPORTED_OPERATORS:
        out.append(f"the rule operator {operator} is not available offline")
    if operator in ("IS_EMPTY", "IS_NOT_EMPTY"):
        return  # presence checks need no comparison, so any source type works
    if item is not None and item.answer_type in registry:
        family = registry.get(item.answer_type).comparison_family
        if family not in SUPPORTED_FAMILIES or item.answer_type == "datetime":
            out.append(f"a rule compares a {item.answer_type} answer, which cannot be evaluated offline")


def compatibility(snapshot: Any, compiled: Any) -> list[str]:
    """Why this version cannot be prepared offline (empty list = fully supported)."""
    reasons: list[str] = []
    for item in snapshot.items.values():
        if not item.active:
            continue
        if item.answer_type not in SUPPORTED_ANSWER_TYPES:
            reasons.append(f"“{item.question}” uses an answer type that is not available offline")
    if compiled.cyclic:
        reasons.append("the checklist's rules cannot be evaluated")
    for rule in compiled.active:
        _condition_problems(rule.stored_condition, snapshot.items, reasons)
        for action in rule.stored_actions:
            kind = action.get("type")
            if kind not in SUPPORTED_ACTIONS:
                reasons.append(f"rule “{rule.name}” uses {kind}, which is not available offline")
    return list(dict.fromkeys(reasons))
