"""Read-side and form helpers of the evaluation screens: the controlled condition builder, readable summaries.  No business logic.

The builder never accepts a path, an expression or code: every choice is picked from a list derived from the registries (``sources.FIELDS``, the version's
questions, the Phase 6 operators valid for the picked field), and the saved rule is validated by ``config.compile_rule`` exactly like one from the API.
A condition is shown as *rows* (a comparison, optionally negated, in the top level or in one of three groups with their own AND/OR); a condition that has a
shape the rows cannot express (created through the API) is shown read-only and kept as it is.
"""

from __future__ import annotations

from typing import Any

from apps.checklists.rules.operators import OPERATORS, operators_for

from . import constants as k
from . import sources
from .config import Group, Leaf, compile_rule

ROWS_DEFAULT = 3
ROWS_MAX = 12
GROUPS = 3
ACTION_ROWS = 3


# ---------------------------------------------------------------------------------------------------------------------------------
# Choices
# ---------------------------------------------------------------------------------------------------------------------------------
def source_choices() -> list[tuple[str, str]]:
    return [("answer", "A question's answer"), ("score", "The score"), ("execution", "The execution")]


def field_choices(snapshot: Any, source: str) -> list[tuple[str, str]]:
    if source == "answer":
        return [(code, f"{code} - {item.question[:70]}") for code, item in snapshot.items.items()]
    return [(f.code, f.label) for f in sources.FIELDS.get(source, {}).values()]


def operator_choices(snapshot: Any, source: str, field: str) -> list[Any]:
    if source == "answer":
        item = snapshot.items.get(field)
        return operators_for(item.answer_type) if item is not None else []
    f = sources.FIELDS.get(source, {}).get(field)
    return [op for op in OPERATORS.values() if f is not None and f.family in op.families]


def value_widget(snapshot: Any, source: str, field: str, op: Any) -> dict[str, Any]:
    """How to ask for the comparison value: ``kind`` is none | text | number | date | select | bool | range | multi."""
    if op is None or op.operand == "none":
        return {"kind": "none"}
    choices: list[tuple[str, str]] = []
    base = "text"
    if source == "answer":
        item = snapshot.items.get(field)
        atype = item.answer_type if item is not None else "text"
        if atype in ("yes_no", "yes_no_na"):
            base, choices = "select", [("yes", "Yes"), ("no", "No")] + ([("na", "N/A")] if atype == "yes_no_na" else [])
        elif atype == "boolean":
            base = "bool"
        elif item is not None and item.definition.comparison_family in ("choice", "multi_choice"):
            base, choices = "select", [(o.value, o.label) for o in item.options]
        elif item is not None and item.definition.comparison_family == "ordered":
            base = "date" if atype in ("date", "datetime") else "number"
    else:
        f = sources.FIELDS.get(source, {}).get(field)
        if f is not None:
            base = {"decimal": "number", "bool": "bool", "choice": "select", "text": "text"}[f.kind]
            choices = [(c, c.replace("_", " ").title()) for c in (f.choices or ())]
    if op.operand == "range":
        return {"kind": "range", "input": base if base in ("number", "date") else "text"}
    if op.operand == "options":
        return {"kind": "multi", "choices": choices}
    return {"kind": base, "choices": choices}


# ---------------------------------------------------------------------------------------------------------------------------------
# Rule <-> form
# ---------------------------------------------------------------------------------------------------------------------------------
def _to_bool(raw: str) -> Any:
    return True if raw == "true" else False if raw == "false" else raw


def _row_value(post: Any, i: int, widget: dict[str, Any], op: Any) -> Any:
    if widget["kind"] == "none":
        return None
    if widget["kind"] == "range":
        return [post.get(f"val_{i}", "").strip(), post.get(f"val2_{i}", "").strip()]
    if widget["kind"] == "multi":
        return post.getlist(f"vals_{i}")
    raw = post.get(f"val_{i}", "").strip()
    return _to_bool(raw) if widget["kind"] == "bool" else raw


def rows_from_post(post: Any, snapshot: Any) -> list[dict[str, Any]]:
    """The condition rows as posted, repaired so every select shows a valid choice (changing a source resets its field and operator)."""
    try:
        count = max(1, min(int(post.get("rows", ROWS_DEFAULT)), ROWS_MAX))
    except ValueError:
        count = ROWS_DEFAULT
    rows = []
    for i in range(count):
        source = post.get(f"src_{i}", "")
        if source not in k.SOURCES:
            rows.append({"i": i, "blank": True, "src": "", "fld": "", "op": "", "group": post.get(f"grp_{i}", "0"), "neg": post.get(f"neg_{i}") == "on"})
            continue
        fields = [c for c, _l in field_choices(snapshot, source)]
        field = post.get(f"fld_{i}", "")
        if field not in fields:
            field = fields[0] if fields else ""
        ops = operator_choices(snapshot, source, field)
        op_code = post.get(f"op_{i}", "")
        op = OPERATORS.get(op_code) if OPERATORS.get(op_code) in ops else (ops[0] if ops else None)
        widget = value_widget(snapshot, source, field, op)
        rows.append({"i": i, "blank": False, "src": source, "fld": field, "op": op.code if op else "", "group": post.get(f"grp_{i}", "0"), "neg": post.get(f"neg_{i}") == "on",
                     "value": _row_value(post, i, widget, op), "val": post.get(f"val_{i}", ""), "val2": post.get(f"val2_{i}", ""), "vals": post.getlist(f"vals_{i}"),
                     "fields": field_choices(snapshot, source), "ops": ops, "widget": widget})
    return rows


def condition_from_form(post: Any, rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The condition tree of the posted rows (``None`` when no row is filled in)."""
    buckets: dict[int, list[dict[str, Any]]] = {g: [] for g in range(GROUPS + 1)}
    for row in rows:
        if row["blank"] or not row["fld"] or not row["op"]:
            continue
        leaf: dict[str, Any] = {"source": row["src"], "field": row["fld"], "operator": row["op"]}
        if row["widget"]["kind"] != "none":
            leaf["value"] = row["value"]
        if row["neg"]:
            leaf = {"logic": "NOT", "conditions": [leaf]}
        try:
            group = int(row["group"])
        except ValueError:
            group = 0
        buckets[group if group in buckets else 0].append(leaf)
    children = list(buckets[0])
    for g in range(1, GROUPS + 1):
        if buckets[g]:
            node: dict[str, Any] = {"logic": "OR" if post.get(f"gl_{g}") == "OR" else "AND", "conditions": buckets[g]}
            if post.get(f"gn_{g}") == "on":
                node = {"logic": "NOT", "conditions": [node]}
            children.append(node)
    if not children:
        return None
    return {"logic": "OR" if post.get("root_logic") == "OR" else "AND", "conditions": children}


def _row_for(leaf: dict[str, Any], group: int, neg: bool, i: int, snapshot: Any) -> dict[str, Any]:
    source, field = leaf.get("source", ""), leaf.get("field", "")
    ops = operator_choices(snapshot, source, field)
    op = OPERATORS.get(str(leaf.get("operator", "")).upper())
    widget = value_widget(snapshot, source, field, op)
    value = leaf.get("value")
    row = {"i": i, "blank": False, "src": source, "fld": field, "op": op.code if op else "", "group": str(group), "neg": neg, "fields": field_choices(snapshot, source), "ops": ops,
           "widget": widget, "val": "", "val2": "", "vals": []}
    if widget["kind"] == "range" and isinstance(value, list) and len(value) == 2:
        row["val"], row["val2"] = str(value[0]), str(value[1])
    elif widget["kind"] == "multi" and isinstance(value, list):
        row["vals"] = [str(v) for v in value]
    elif value is not None:
        row["val"] = ("true" if value else "false") if isinstance(value, bool) else str(value)
    return row


def rows_from_condition(condition: Any, snapshot: Any) -> tuple[list[dict[str, Any]], dict[str, Any], bool]:
    """``(rows, group settings, fits)`` of a stored condition; ``fits`` is False when its shape cannot be shown as rows (kept unchanged on save)."""
    settings = {"root_logic": "AND", "gl": {1: "AND", 2: "AND", 3: "AND"}, "gn": {1: False, 2: False, 3: False}}
    rows: list[dict[str, Any]] = []
    if not condition:
        return rows, settings, True
    if not isinstance(condition, dict) or "logic" not in condition or condition.get("logic") == "NOT":
        condition = {"logic": "AND", "conditions": [condition]}
    settings["root_logic"] = condition["logic"] if condition["logic"] in ("AND", "OR") else "AND"

    def leaf_of(node: Any) -> tuple[dict[str, Any], bool] | None:
        if isinstance(node, dict) and "logic" not in node:
            return node, False
        if isinstance(node, dict) and node.get("logic") == "NOT" and len(node.get("conditions", [])) == 1 and "logic" not in node["conditions"][0]:
            return node["conditions"][0], True
        return None

    group_no = 0
    for child in condition.get("conditions", []):
        as_leaf = leaf_of(child)
        if as_leaf is not None:
            rows.append(_row_for(as_leaf[0], 0, as_leaf[1], len(rows), snapshot))
            continue
        negated = False
        node = child
        if isinstance(node, dict) and node.get("logic") == "NOT" and len(node.get("conditions", [])) == 1:
            negated, node = True, node["conditions"][0]
        if not isinstance(node, dict) or node.get("logic") not in ("AND", "OR") or group_no >= GROUPS:
            return [], settings, False
        group_no += 1
        settings["gl"][group_no], settings["gn"][group_no] = node["logic"], negated
        for grand in node.get("conditions", []):
            inner = leaf_of(grand)
            if inner is None:
                return [], settings, False
            rows.append(_row_for(inner[0], group_no, inner[1], len(rows), snapshot))
    return rows, settings, True


def pad_rows(rows: list[dict[str, Any]], snapshot: Any, count: int = ROWS_DEFAULT) -> list[dict[str, Any]]:
    out = list(rows)
    while len(out) < count:
        out.append({"i": len(out), "blank": True, "src": "", "fld": "", "op": "", "group": "0", "neg": False})
    return out


def blank_row_view(row: dict[str, Any]) -> dict[str, Any]:
    return row


# ---------------------------------------------------------------------------------------------------------------------------------
# Actions rows
# ---------------------------------------------------------------------------------------------------------------------------------
def actions_from_post(post: Any) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for j in range(ACTION_ROWS):
        kind = post.get(f"act_type_{j}", "")
        if not kind:
            continue
        if kind == k.ActionType.REQUEST_FINDING:
            out.append({"type": kind, "category": post.get(f"act_cat_{j}", "").strip(), "severity": post.get(f"act_sev_{j}", "").strip(), "title": post.get(f"act_title_{j}", "").strip(),
                        "description": post.get(f"act_desc_{j}", "").strip(), "requires_review": post.get(f"act_review_{j}") == "on"})
        elif kind == k.ActionType.START_WORKFLOW:
            out.append({"type": kind, "workflow": post.get(f"act_wf_{j}", "").strip()})
        else:
            out.append({"type": kind, "tag": post.get(f"act_tag_{j}", "").strip()})
    return out


def action_rows(actions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows = [{"j": j, **a} for j, a in enumerate(actions[:ACTION_ROWS])]
    while len(rows) < ACTION_ROWS:
        rows.append({"j": len(rows), "type": ""})
    return rows


# ---------------------------------------------------------------------------------------------------------------------------------
# Readable summaries
# ---------------------------------------------------------------------------------------------------------------------------------
def render_node(node: Any) -> str:
    from .engine import leaf_text

    if isinstance(node, Group):
        parts = [render_node(c) for c in node.children]
        if node.logic == "NOT":
            return f"not ({parts[0]})"
        return "(" + f" {node.logic.lower()} ".join(parts) + ")" if len(parts) > 1 else parts[0]
    if isinstance(node, Leaf):
        return leaf_text(node)
    return "?"


def catalogue(snapshot: Any, document: dict[str, Any], issues: list[Any]) -> list[dict[str, Any]]:
    """The rules in priority order with a readable condition, the actions and the issues that concern each."""
    by_subject: dict[str, list[Any]] = {}
    for issue in issues:
        by_subject.setdefault(issue.subject, []).append(issue)
    out = []
    for raw in document.get("rules", []):
        if not isinstance(raw, dict):
            continue
        scratch: list[Any] = []
        compiled = compile_rule(snapshot, raw, 0, None, scratch)
        code = str(raw.get("code", ""))
        out.append({"code": code, "name": raw.get("name", code), "priority": raw.get("priority"), "enabled": raw.get("enabled", True), "outcome": raw.get("outcome", ""),
                    "outcome_label": dict(k.Outcome.choices).get(raw.get("outcome", ""), raw.get("outcome", "")),
                    "condition": render_node(compiled.node) if compiled is not None else "This rule has problems and cannot be read yet.",
                    "actions": [str(dict(k.ActionType.choices).get(a.get("type"), a.get("type"))) + (f": {a.get('title') or a.get('workflow') or a.get('tag')}" if isinstance(a, dict) else "")
                                for a in raw.get("actions", []) if isinstance(a, dict)],
                    "stop": bool(raw.get("stop")), "message": raw.get("message", ""), "issues": by_subject.get(f"rule:{code}", [])})
    return out


def sample_fields(snapshot: Any, document: dict[str, Any]) -> list[dict[str, Any]]:
    """The inputs of the what-if form: the questions the rules read."""
    wanted: list[str] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            if node.get("source") == "answer" and node.get("field") in snapshot.items and node["field"] not in wanted:
                wanted.append(node["field"])
            for child in node.get("conditions", []) if isinstance(node.get("conditions"), list) else []:
                walk(child)

    for rule in document.get("rules", []):
        if isinstance(rule, dict):
            walk(rule.get("condition"))
    out = []
    for code in wanted:
        item = snapshot.items[code]
        widget = value_widget(snapshot, "answer", code, OPERATORS["EQUALS"] if item.definition.comparison_family != "multi_choice" else OPERATORS["SELECTED"])
        out.append({"code": code, "question": item.question, "type": item.answer_type, "widget": widget, "multi": item.answer_type == "multiple_choice"})
    return out
