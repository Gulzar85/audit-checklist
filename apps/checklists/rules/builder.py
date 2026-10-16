"""The rule builder's form model: posted fields <-> a small state tree <-> the stored rule JSON.

The builder UI never asks an author to type JSON.  Each request carries the whole editor as ordinary form fields; this
module turns them into a *state* (plain dicts), applies the requested operation (add/remove a condition, group or
action), repairs anything that no longer makes sense (a different question was chosen, so the operator and value are
reset), and builds the rule payload.  The payload is then validated and saved by the same service as the API, so the
editor - including its "advanced JSON" panel - cannot bypass any rule validation.

Field names (``<row>`` is ``c<i>`` for a root condition and ``g<j>c<k>`` for a condition in a group)::

    name, description, logic              the rule and its root AND/OR
    <row>_src / _op / _val / _val2 / _vals    question, comparison, value (or range ends / chosen options)
    g<j>_logic                            AND/OR of a nested group
    a<i>_type / _target / _<param>        an action, its target ("item:CODE" | "section:CODE") and its parameters

The editor supports a root group plus one level of nested groups; a stored rule that is nested deeper (possible through
the API) is shown read-only as JSON-editable (``json_only``) instead of being flattened.
"""

from __future__ import annotations

import json
import re
from typing import Any

from .. import constants as c
from .actions import ACTIONS, EVIDENCE_KINDS, SEVERITIES
from .limits import limit
from .operators import OPERATORS, operators_for
from .snapshot import ItemSpec, VersionSnapshot

MAX_JSON_CHARS = 50_000
MAX_FIELD_CHARS = 2_000
MAX_UI_GROUPS = 10
_FIELD = re.compile(r"^(?:c(\d+)|g(\d+)c(\d+)|g(\d+)|a(\d+))_([a-z0-9_]+)$")
_OPS = ("refresh", "add_condition", "remove_condition", "add_group", "remove_group", "add_action", "remove_action",
        "apply_json", "save")

NUMBER_KINDS = {"int": "1", "number": "any"}

#: How the action picker groups the registry's actions (labels come from the registry itself).
ACTION_GROUPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Visibility", ("SHOW_ITEM", "HIDE_ITEM", "SHOW_SECTION", "HIDE_SECTION")),
    ("Requirements", ("REQUIRE_ITEM", "MAKE_OPTIONAL", "REQUIRE_COMMENT", "REQUIRE_EVIDENCE")),
    ("Guidance", ("DISPLAY_MESSAGE", "ADD_INSTRUCTIONS")),
    ("Validation", ("SET_VALIDATION_CONSTRAINT", "OPTION_AVAILABILITY")),
)


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------
def blank_condition() -> dict[str, Any]:
    return {"src": "", "op": "", "val": "", "val2": "", "vals": [], "error": ""}


def blank_group() -> dict[str, Any]:
    return {"logic": "AND", "conditions": [blank_condition()], "error": ""}


def blank_action() -> dict[str, Any]:
    return {"type": "", "target": "", "p": {}, "error": ""}


def blank_state() -> dict[str, Any]:
    return {"name": "", "description": "", "logic": "AND", "conditions": [blank_condition()], "groups": [], "actions": [blank_action()],
            "json_only": False, "json_input": "", "errors": [], "warnings": [], "notice": ""}


def _text(post: Any, key: str, maximum: int = MAX_FIELD_CHARS) -> str:
    return str(post.get(key, ""))[:maximum]


def _many(post: Any, key: str) -> list[str]:
    values = post.getlist(key) if hasattr(post, "getlist") else list(post.get(key, []))
    return [str(v)[:200] for v in values[: limit("max_operand_items")]]


def _indices(post: Any, pattern: re.Pattern[str], group: int) -> list[int]:
    found = {int(m.group(group)) for key in post if (m := pattern.match(key))}
    return sorted(found)


def parse_post(post: Any) -> dict[str, Any]:
    """Read the editor's fields into a state (bounded: counts and lengths are capped, unknown fields ignored)."""
    state = blank_state()
    state.update(name=_text(post, "name", 200), description=_text(post, "description", 400),
                 logic="OR" if post.get("logic") == "OR" else "AND", json_input=_text(post, "json_input", MAX_JSON_CHARS))
    state["json_only"] = post.get("json_only") == "1"
    max_rows = limit("max_conditions_per_rule")

    def condition(prefix: str) -> dict[str, Any]:
        return {"src": _text(post, f"{prefix}_src", 100), "op": _text(post, f"{prefix}_op", 40), "val": _text(post, f"{prefix}_val"),
                "val2": _text(post, f"{prefix}_val2"), "vals": _many(post, f"{prefix}_vals"), "error": ""}

    state["conditions"] = [condition(f"c{i}") for i in _indices(post, re.compile(r"^c(\d+)_src$"), 1)[:max_rows]]
    groups = []
    for j in _indices(post, re.compile(r"^g(\d+)_logic$"), 1)[:MAX_UI_GROUPS]:
        rows = _indices(post, re.compile(rf"^g{j}c(\d+)_src$"), 1)[:max_rows]
        groups.append({"logic": "OR" if post.get(f"g{j}_logic") == "OR" else "AND", "error": "",
                       "conditions": [condition(f"g{j}c{k}") for k in rows]})
    state["groups"] = groups
    actions = []
    for i in _indices(post, re.compile(r"^a(\d+)_type$"), 1)[: limit("max_actions_per_rule")]:
        params: dict[str, Any] = {}
        for key in post:
            m = _FIELD.match(key)
            if m and m.group(5) is not None and int(m.group(5)) == i and m.group(6) != "type" and m.group(6) != "target":
                name = m.group(6)
                params[name] = _many(post, key) if name in ("kinds", "options") else _text(post, key)
        actions.append({"type": _text(post, f"a{i}_type", 40), "target": _text(post, f"a{i}_target", 120), "p": params, "error": ""})
    state["actions"] = actions
    return state


def _scalar_text(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return "" if value is None else str(value)


def _row_from_node(node: dict[str, Any]) -> dict[str, Any]:
    row = blank_condition()
    row.update(src=str(node.get("source_item_code", "")), op=str(node.get("comparison", "")))
    value = node.get("value")
    if isinstance(value, list) and OPERATORS.get(row["op"]) and OPERATORS[row["op"]].operand == "range" and len(value) == 2:
        row["val"], row["val2"] = _scalar_text(value[0]), _scalar_text(value[1])
    elif isinstance(value, list):
        row["vals"] = [_scalar_text(v) for v in value]
    else:
        row["val"] = _scalar_text(value)
    return row


def _action_from_stored(action: dict[str, Any]) -> dict[str, Any]:
    target = action.get("target") or {}
    params = {k: v for k, v in action.items() if k not in ("type", "target")}
    flat: dict[str, Any] = {}
    for key, value in params.items():
        if key == "constraints" and isinstance(value, dict):
            for ckey, cval in value.items():
                flat[f"cons_{ckey}"] = _scalar_text(cval)
        elif isinstance(value, list):
            flat[key] = [_scalar_text(v) for v in value]
        else:
            flat[key] = _scalar_text(value)
    return {"type": str(action.get("type", "")), "target": f"{target['type']}:{target['code']}" if target else "", "p": flat, "error": ""}


def state_from_rule(name: str, description: str, condition: Any, actions: Any) -> dict[str, Any]:
    """The editor state of a stored rule (``json_only`` when its nesting is deeper than the editor shows)."""
    state = blank_state()
    state.update(name=name, description=description, conditions=[], groups=[])
    if isinstance(condition, dict) and "conditions" in condition:
        state["logic"] = condition.get("operator", "AND")
        for child in condition["conditions"]:
            if "conditions" in child:
                group = {"logic": child.get("operator", "AND"), "conditions": [], "error": ""}
                for sub in child["conditions"]:
                    if "conditions" in sub:
                        state["json_only"] = True
                    else:
                        group["conditions"].append(_row_from_node(sub))
                state["groups"].append(group)
            else:
                state["conditions"].append(_row_from_node(child))
    elif isinstance(condition, dict) and condition:
        state["conditions"].append(_row_from_node(condition))
    state["actions"] = [_action_from_stored(a) for a in actions if isinstance(a, dict)] if isinstance(actions, list) else []
    state["json_input"] = json.dumps({"condition": condition, "actions": actions}, indent=2, ensure_ascii=False)
    return state


# ---------------------------------------------------------------------------
# Operations (add / remove rows, apply JSON)
# ---------------------------------------------------------------------------
def split_operation(raw: str) -> tuple[str, list[str]]:
    name, *args = (raw or "refresh").split(":")
    return (name, args) if name in _OPS else ("refresh", [])


def _index(args: list[str], position: int, size: int) -> int | None:
    try:
        value = int(args[position])
    except (IndexError, ValueError):
        return None
    return value if 0 <= value < size else None


def apply_operation(state: dict[str, Any], name: str, args: list[str]) -> None:
    """Mutate ``state`` for one structural operation.  Limits are enforced here, not just hidden in the template."""
    total = len(state["conditions"]) + sum(len(g["conditions"]) for g in state["groups"])
    if name == "add_condition":
        if total >= limit("max_conditions_per_rule"):
            state["notice"] = f"A rule may have at most {limit('max_conditions_per_rule')} conditions."
        elif args and args[0] == "root":
            state["conditions"].append(blank_condition())
        elif (j := _index(args, 0, len(state["groups"]))) is not None:
            state["groups"][j]["conditions"].append(blank_condition())
    elif name == "remove_condition":
        if args and args[0] == "root":
            if (i := _index(args, 1, len(state["conditions"]))) is not None:
                del state["conditions"][i]
        elif (j := _index(args, 0, len(state["groups"]))) is not None:
            if (k := _index(args, 1, len(state["groups"][j]["conditions"]))) is not None:
                del state["groups"][j]["conditions"][k]
    elif name == "add_group":
        if len(state["groups"]) >= MAX_UI_GROUPS or total >= limit("max_conditions_per_rule"):
            state["notice"] = "No more groups can be added to this rule."
        elif limit("max_group_depth") < 2:
            state["notice"] = "Groups are disabled by the configured depth limit."
        else:
            state["groups"].append(blank_group())
    elif name == "remove_group":
        if (j := _index(args, 0, len(state["groups"]))) is not None:
            del state["groups"][j]
    elif name == "add_action":
        if len(state["actions"]) >= limit("max_actions_per_rule"):
            state["notice"] = f"A rule may have at most {limit('max_actions_per_rule')} actions."
        else:
            state["actions"].append(blank_action())
    elif name == "remove_action":
        if (i := _index(args, 0, len(state["actions"]))) is not None:
            del state["actions"][i]


# ---------------------------------------------------------------------------
# Controls: what each row offers, given the chosen question
# ---------------------------------------------------------------------------
def value_widget(item: ItemSpec) -> dict[str, Any]:
    """How the comparison value of a question is entered (a select for closed answers, a typed input otherwise)."""
    code, family = item.answer_type, item.definition.comparison_family
    if code == c.AnswerType.BOOLEAN:
        return {"kind": "select", "choices": [("true", "True"), ("false", "False")]}
    if code in c.FIXED_OPTIONS:
        return {"kind": "select", "choices": list(c.FIXED_OPTIONS[code])}
    if family in ("choice", "multi_choice"):
        return {"kind": "select", "choices": [(o.value, o.label) for o in item.options if o.is_active]}
    if code == c.AnswerType.DATE:
        return {"kind": "date"}
    if code == c.AnswerType.DATETIME:
        return {"kind": "datetime-local"}
    if code == c.AnswerType.SCORE:
        return {"kind": "number", "step": "1", "unit": ""}
    if family == "ordered":
        return {"kind": "number", "step": "any", "unit": item.config.get("unit", "")}
    return {"kind": "text"}


def constraint_fields(item: ItemSpec) -> list[dict[str, Any]]:
    """The settings a rule may tighten for ``item`` (declared by its answer type), with the question's own limits."""
    fields = {f.key: f for f in item.definition.config_fields}
    out = []
    for key in item.definition.rule_constraints:
        spec = fields.get(key)
        if spec is None:
            continue
        kind = {"int": "number", "number": "number", "date": "date", "datetime": "datetime-local"}.get(spec.kind, "text")
        out.append({"key": key, "label": spec.label, "kind": kind, "step": NUMBER_KINDS.get(spec.kind, ""), "own": item.config.get(key)})
    return out


def _item_for(snapshot: VersionSnapshot, target: str) -> ItemSpec | None:
    kind, _, code = target.partition(":")
    return snapshot.items.get(code) if kind == "item" else None


def normalize_state(snapshot: VersionSnapshot, state: dict[str, Any]) -> None:
    """Repair choices that no longer fit (new question -> operator/value reset), so the editor never holds an invalid combo."""
    for row in [*state["conditions"], *(r for g in state["groups"] for r in g["conditions"])]:
        item = snapshot.items.get(row["src"])
        if item is None:
            row.update(src="", op="", val="", val2="", vals=[])
            continue
        allowed = {op.code: op for op in operators_for(item.answer_type)}
        if row["op"] not in allowed:
            row["op"] = next(iter(allowed), "")
            row.update(val="", val2="", vals=[])
        widget = value_widget(item)
        if widget["kind"] == "select":
            valid = {value for value, _label in widget["choices"]}
            if row["val"] not in valid:
                row["val"] = ""
            row["vals"] = [v for v in row["vals"] if v in valid]
    for action in state["actions"]:
        definition = ACTIONS.get(action["type"])
        if definition is None:
            action["type"] = ""
            action["target"] = ""
            continue
        kind, _, code = action["target"].partition(":")
        wanted = ("item", "section") if definition.target == "optional" else (definition.target,)
        known = snapshot.items if kind == "item" else snapshot.sections if kind == "section" else {}
        if kind not in wanted or code not in known or (kind == "item" and not _supports(definition.code, known[code])):
            action["target"] = ""
        item = _item_for(snapshot, action["target"])
        if action["type"] == "OPTION_AVAILABILITY":
            valid = {o.value for o in item.options if o.is_active} if item else set()
            action["p"]["options"] = [v for v in action["p"].get("options", []) if v in valid]


def condition_rows(snapshot: VersionSnapshot, state: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """View models of the root rows and of each group's rows (controls resolved for the chosen question)."""

    def row_view(prefix: str, row: dict[str, Any], remove: str) -> dict[str, Any]:
        item = snapshot.items.get(row["src"])
        operators = [(op.code, op.label) for op in operators_for(item.answer_type)] if item else []
        operand = OPERATORS[row["op"]].operand if row["op"] in OPERATORS else "none"
        return {**row, "prefix": prefix, "item": item, "operators": operators, "operand": operand,
                "widget": value_widget(item) if item else {"kind": "text"}, "remove": remove,
                "selected": set(row["vals"])}

    roots = [row_view(f"c{i}", row, f"root:{i}") for i, row in enumerate(state["conditions"])]
    groups = [{**g, "index": j, "rows": [row_view(f"g{j}c{k}", row, f"{j}:{k}") for k, row in enumerate(g["conditions"])]}
              for j, g in enumerate(state["groups"])]
    return roots, groups


def _supports(action_code: str, item: ItemSpec) -> bool:
    """Can an action of this type target this question?  (The validator would refuse the rest, so the picker never offers it.)"""
    definition = item.definition
    if action_code == "OPTION_AVAILABILITY":
        return definition.comparison_family in ("choice", "multi_choice") and not definition.capabilities.has_fixed_options
    if action_code == "SET_VALIDATION_CONSTRAINT":
        return bool(definition.rule_constraints)
    return True


def source_choices(snapshot: VersionSnapshot, action_code: str = "") -> list[tuple[str, list[tuple[str, str]]]]:
    """Questions grouped by section, in checklist order (only those an ``action_code`` action can target, when given)."""
    by_section: dict[str, list[tuple[str, str]]] = {}
    for item in sorted(snapshot.items.values(), key=lambda i: (snapshot.sections[i.section_code].sort_order, i.sort_order)):
        if action_code and not _supports(action_code, item):
            continue
        label = f"{item.code} · {item.question[:70]}" + ("" if item.active else " (inactive)")
        by_section.setdefault(item.section_code, []).append((item.code, label))
    return [(f"{snapshot.sections[code].name}", rows) for code, rows in by_section.items()]


def action_rows(snapshot: VersionSnapshot, state: dict[str, Any]) -> list[dict[str, Any]]:
    sections = [(s.code, f"{s.code} · {s.name}") for s in sorted(snapshot.sections.values(), key=lambda s: s.sort_order)]
    out = []
    for i, action in enumerate(state["actions"]):
        definition = ACTIONS.get(action["type"])
        item = _item_for(snapshot, action["target"])
        p = action["p"]
        row = {**action, "prefix": f"a{i}", "index": i, "definition": definition, "target_kind": action["target"].partition(":")[0],
               "item": item, "p": p, "selected_kinds": set(p.get("kinds") or ["photo"]),
               "selected_options": set(p.get("options") or [])}
        if definition is None:
            row["target_groups"] = []
        else:
            groups: list[tuple[str, list[tuple[str, str]]]] = []
            if definition.target in ("item", "optional"):
                groups.extend(("Questions · " + name, [(f"item:{code}", label) for code, label in rows])
                              for name, rows in source_choices(snapshot, definition.code))
            if definition.target in ("section", "optional"):
                groups.append(("Sections", [(f"section:{code}", label) for code, label in sections]))
            row["target_groups"] = groups
        if action["type"] == "SET_VALIDATION_CONSTRAINT" and item is not None:
            row["constraint_fields"] = [{**f, "value": p.get(f"cons_{f['key']}", "")} for f in constraint_fields(item)]
        if action["type"] == "OPTION_AVAILABILITY" and item is not None:
            row["option_choices"] = [(o.value, o.label) for o in item.options if o.is_active]
        out.append(row)
    return out


# ---------------------------------------------------------------------------
# State -> payload
# ---------------------------------------------------------------------------
def _condition_node(row: dict[str, Any], strict: bool) -> dict[str, Any] | None:
    if not row["src"]:
        if strict:
            row["error"] = "Choose a question."
        return None
    operator = OPERATORS.get(row["op"])
    if operator is None:
        row["error"] = "Choose a comparison."
        return None
    node: dict[str, Any] = {"source_item_code": row["src"], "comparison": operator.code}
    if operator.operand in ("scalar", "option", "text"):
        node["value"] = row["val"].strip() if operator.operand == "text" else row["val"]
    elif operator.operand == "range":
        node["value"] = [row["val"], row["val2"]]
    elif operator.operand == "options":
        node["value"] = list(row["vals"])
    return node


def build_condition(state: dict[str, Any], *, strict: bool) -> dict[str, Any] | None:
    """The stored-form condition tree (``None`` means "always").  Problems are written onto the state's rows."""
    children: list[dict[str, Any]] = []
    for row in state["conditions"]:
        row["error"] = ""
        if node := _condition_node(row, strict):
            children.append(node)
    for group in state["groups"]:
        group["error"] = ""
        nodes = []
        for row in group["conditions"]:
            row["error"] = ""
            if node := _condition_node(row, strict):
                nodes.append(node)
        if not group["conditions"] and strict:
            group["error"] = "A group needs at least one condition."
        if nodes:
            children.append({"operator": group["logic"], "conditions": nodes})
    return {"operator": state["logic"], "conditions": children} if children else None


def build_actions(snapshot: VersionSnapshot, state: dict[str, Any], *, strict: bool) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for action in state["actions"]:
        action["error"] = ""
        definition = ACTIONS.get(action["type"])
        if definition is None:
            if strict:
                action["error"] = "Choose what the rule should do."
            continue
        payload: dict[str, Any] = {"type": definition.code}
        kind, _, code = action["target"].partition(":")
        if code:
            payload["target"] = {"type": kind, "code": code}
        elif definition.target != "optional" and strict:
            action["error"] = "Choose the question or section this applies to."
            continue
        p = action["p"]
        code_ = definition.code
        if code_ == "DISPLAY_MESSAGE":
            payload.update(severity=p.get("severity") or "info", message=p.get("message", ""))
        elif code_ == "REQUIRE_COMMENT":
            if p.get("prompt", "").strip():
                payload["prompt"] = p["prompt"].strip()
        elif code_ == "REQUIRE_EVIDENCE":
            payload["kinds"] = [k for k in p.get("kinds", []) if k in EVIDENCE_KINDS] or ["photo"]
            try:
                payload["min_count"] = int(p.get("min_count") or 1)
            except ValueError:
                action["error"] = "The number of files must be a whole number."
                continue
        elif code_ == "ADD_INSTRUCTIONS":
            payload["text"] = p.get("text", "")
        elif code_ == "OPTION_AVAILABILITY":
            payload.update(mode=p.get("mode") or "only", options=list(p.get("options", [])))
        elif code_ == "SET_VALIDATION_CONSTRAINT":
            constraints = {k[5:]: v.strip() for k, v in p.items() if k.startswith("cons_") and isinstance(v, str) and v.strip()}
            payload["constraints"] = constraints
        out.append(payload)
    return out


def all_rows(state: dict[str, Any]) -> list[dict[str, Any]]:
    return [*state["conditions"], *(row for group in state["groups"] for row in group["conditions"])]


def has_row_errors(state: dict[str, Any]) -> bool:
    return any(r["error"] for r in all_rows(state)) or any(g["error"] for g in state["groups"]) or any(a["error"] for a in state["actions"])


def action_groups() -> list[tuple[str, list[tuple[str, str]]]]:
    return [(label, [(code, ACTIONS[code].label) for code in codes]) for label, codes in ACTION_GROUPS]


def ready(state: dict[str, Any]) -> bool:
    """Is every started row complete enough to be checked?  (Checking half-filled rows would only produce noise.)"""
    for row in all_rows(state):
        if not row["src"]:
            continue  # an untouched row is simply skipped
        operator = OPERATORS.get(row["op"])
        if operator is None:
            return False
        if operator.operand in ("scalar", "option", "text") and not row["val"].strip():
            return False
        if operator.operand == "range" and not (row["val"].strip() and row["val2"].strip()):
            return False
        if operator.operand == "options" and not row["vals"]:
            return False
    if not state["actions"]:
        return False
    for action in state["actions"]:
        definition = ACTIONS.get(action["type"])
        if definition is None or (not action["target"] and definition.target != "optional"):
            return False
        p = action["p"]
        if definition.code == "DISPLAY_MESSAGE" and not p.get("message", "").strip():
            return False
        if definition.code == "ADD_INSTRUCTIONS" and not p.get("text", "").strip():
            return False
        if definition.code == "OPTION_AVAILABILITY" and not p.get("options"):
            return False
        if definition.code == "SET_VALIDATION_CONSTRAINT" and not any(k.startswith("cons_") and str(v).strip() for k, v in p.items()):
            return False
    return True


# ---------------------------------------------------------------------------
# Issues -> rows
# ---------------------------------------------------------------------------
_COND = re.compile(r"conditions\[(\d+)\](?:\.conditions\[(\d+)\])?")
_ACT = re.compile(r"^actions\[(\d+)\]")


def attach_issues(state: dict[str, Any], issues: list[Any], shape: dict[str, Any]) -> list[str]:
    """Write each issue on the row it belongs to (by its path); the rest are returned as rule-level messages.

    ``shape`` (from :func:`build_payload`) remembers which rows became which payload entries: included root rows come
    first in the payload, then the included groups.
    """
    general: list[str] = []
    roots, groups, actions = shape["roots"], shape["groups"], shape["actions"]
    for issue in issues:
        message, path = issue.message, getattr(issue, "path", "") or ""
        if (a := _ACT.match(path)) and int(a.group(1)) < len(actions):
            state["actions"][actions[int(a.group(1))]]["error"] = message
            continue
        if path.startswith("condition") and (m := _COND.search(path)):
            first = int(m.group(1))
            if m.group(2) is None and first < len(roots):
                state["conditions"][roots[first]]["error"] = message
                continue
            if m.group(2) is not None and 0 <= first - len(roots) < len(groups):
                j, rows = groups[first - len(roots)]
                if int(m.group(2)) < len(rows):
                    state["groups"][j]["conditions"][rows[int(m.group(2))]]["error"] = message
                    continue
        general.append(message)
    return general


def _complete(row: dict[str, Any]) -> bool:
    return bool(row["src"] and OPERATORS.get(row["op"]))


def build_payload(snapshot: VersionSnapshot, state: dict[str, Any], *, strict: bool) -> tuple[dict[str, Any] | None, list[dict[str, Any]], dict[str, Any]]:
    """``(condition, actions, shape)``.  ``strict`` (a save attempt) also reports unfinished rows; otherwise they are skipped."""
    normalize_state(snapshot, state)
    condition = build_condition(state, strict=strict)
    actions = build_actions(snapshot, state, strict=strict)
    shape = {
        "roots": [i for i, row in enumerate(state["conditions"]) if _complete(row)],
        "groups": [(j, [k for k, row in enumerate(g["conditions"]) if _complete(row)]) for j, g in enumerate(state["groups"])
                   if any(_complete(row) for row in g["conditions"])],
        "actions": [i for i, a in enumerate(state["actions"]) if ACTIONS.get(a["type"]) and (a["target"] or ACTIONS[a["type"]].target == "optional")],
    }
    return condition, actions, shape


def parse_json_input(text: str) -> tuple[dict[str, Any] | None, str]:
    """Parse the advanced-JSON panel: ``{"condition": ..., "actions": [...]}``.  Returns ``(data, error)``."""
    if len(text) > MAX_JSON_CHARS:
        return None, f"The JSON may be at most {MAX_JSON_CHARS} characters."
    try:
        data = json.loads(text or "{}", parse_constant=lambda _c: (_ for _ in ()).throw(ValueError("constant")))
    except (ValueError, RecursionError):
        return None, "That is not valid JSON."
    if not isinstance(data, dict) or set(data) - {"condition", "actions"}:
        return None, "Use an object with only “condition” and “actions”."
    if "actions" in data and not isinstance(data["actions"], list):
        return None, "“actions” must be a list."
    return data, ""


__all__ = [
    "ACTION_GROUPS", "EVIDENCE_KINDS", "MAX_JSON_CHARS", "MAX_UI_GROUPS", "SEVERITIES", "action_groups", "action_rows", "all_rows", "apply_operation", "attach_issues",
    "blank_state", "build_payload", "condition_rows", "has_row_errors", "normalize_state", "parse_json_input", "parse_post", "ready",
    "source_choices", "split_operation", "state_from_rule",
]
