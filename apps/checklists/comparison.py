"""Compare two versions of one checklist: what a user (or an auditor) needs to know changed.

The comparison is a pure function of two *manifests* (``manifest.py``), so it needs no database, never alters either version
and is trivial to test.  Everything is matched by **stable code**, never by primary key (a cloned child has a new id):

* sections by ``code`` (``S-001``), items by ``code`` (unique across the version), options by ``value`` within an item,
  rules by ``code`` (``R-001``);
* a matched item is *not* "unchanged" just because its code matches - each behaviour-relevant field is compared, so the same
  code with a different question, answer type, range, options or requiredness is reported as modified;
* order changes are reported as moves of the *fewest* elements (the longest unchanged run keeps its place), so moving one
  section does not report every other section as moved.

Each :class:`Change` carries a ``category`` (section / item / option / rule), a ``kind`` (added / removed / modified /
reordered) and an ``impact``: ``behavior`` (changes what is asked, accepted, required or shown), ``wording`` (text only) or
``structure`` (additions, removals and order).  Filters on these three make a long diff reviewable.
"""

from __future__ import annotations

from bisect import bisect_left
from dataclasses import dataclass, field
from typing import Any

from .rules.actions import ACTIONS
from .rules.operators import OPERATORS

CATEGORIES = ("section", "item", "option", "rule", "scoring", "evaluation")
KINDS = ("added", "removed", "modified", "reordered")
IMPACTS = ("behavior", "wording", "structure")

_SECTION_FIELDS = {"name": "wording", "description": "wording", "instructions": "wording", "is_required": "behavior"}
_ITEM_FIELDS = {
    "question": "wording", "description": "wording", "help_text": "wording", "instructions": "wording",
    "answer_type": "behavior", "is_required": "behavior", "is_active": "behavior", "metadata": "behavior",
}
_RULE_FIELDS = {"name": "wording", "description": "wording", "is_active": "behavior", "condition": "behavior", "actions": "behavior"}
_LABELS = {
    "name": "name", "description": "description", "instructions": "instructions", "is_required": "required", "question": "question text",
    "help_text": "help text", "answer_type": "answer type", "is_active": "active", "metadata": "reference data", "condition": "condition",
    "actions": "actions",
}
_VALUE_WORDS = {"yes": "Yes", "no": "No", "na": "N/A", True: "Yes", False: "No"}


@dataclass(frozen=True)
class Change:
    category: str
    kind: str
    code: str
    label: str
    summary: str
    impact: str = "structure"
    field: str = ""
    before: Any = None
    after: Any = None
    context: str = ""

    def as_dict(self) -> dict[str, Any]:
        out = {"category": self.category, "kind": self.kind, "code": self.code, "label": self.label, "summary": self.summary,
               "impact": self.impact}
        if self.field:
            out.update(field=self.field, before=self.before, after=self.after)
        if self.context:
            out["context"] = self.context
        return out


@dataclass
class Comparison:
    changes: list[Change] = field(default_factory=list)

    @property
    def identical(self) -> bool:
        return not self.changes

    def counts(self) -> dict[str, Any]:
        by_category = {c: sum(1 for ch in self.changes if ch.category == c) for c in CATEGORIES}
        by_kind = {k: sum(1 for ch in self.changes if ch.kind == k) for k in KINDS}
        by_impact = {i: sum(1 for ch in self.changes if ch.impact == i) for i in IMPACTS}
        return {"total": len(self.changes), "by_category": by_category, "by_kind": by_kind, "by_impact": by_impact}

    def filtered(self, *, category: str = "", kind: str = "", impact: str = "") -> list[Change]:
        return [c for c in self.changes
                if (not category or c.category == category) and (not kind or c.kind == kind) and (not impact or c.impact == impact)]

    def as_dict(self) -> dict[str, Any]:
        return {"identical": self.identical, "counts": self.counts(), "changes": [c.as_dict() for c in self.changes]}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _moved(before: list[str], after: list[str]) -> list[str]:
    """Codes that changed relative position, as few as possible (everything on the longest unchanged run stays put)."""
    in_after = set(after)
    in_before = set(before)
    a = [c for c in before if c in in_after]
    position = {c: i for i, c in enumerate(c for c in after if c in in_before)}
    sequence = [position[c] for c in a]
    tails: list[int] = []  # patience sorting with predecessor links to recover one longest increasing run
    tail_index: list[int] = []
    previous = [-1] * len(sequence)
    for i, value in enumerate(sequence):
        j = bisect_left(tails, value)
        if j == len(tails):
            tails.append(value)
            tail_index.append(i)
        else:
            tails[j] = value
            tail_index[j] = i
        previous[i] = tail_index[j - 1] if j else -1
    keep: set[int] = set()
    k = tail_index[-1] if tail_index else -1
    while k != -1:
        keep.add(k)
        k = previous[k]
    moved = {a[i] for i in range(len(a)) if i not in keep}
    return [c for c in after if c in moved]


def _short(value: Any, limit: int = 80) -> str:
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if value is None or value == "":
        return "(empty)"
    text = value if isinstance(value, str) else str(value)
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _quote(text: str) -> str:
    return f"“{_short(text, 60)}”"


class _Describer:
    """Plain-language text for conditions and actions, using the questions of one manifest."""

    def __init__(self, manifest: dict[str, Any]) -> None:
        self.questions = {i["code"]: i["question"] for s in manifest["sections"] for i in s["items"]}
        self.sections = {s["code"]: s["name"] for s in manifest["sections"]}

    def question(self, code: str) -> str:
        return _quote(self.questions[code]) if code in self.questions else code

    def value(self, value: Any) -> str:
        if isinstance(value, list):
            return ", ".join(self.value(v) for v in value)
        if isinstance(value, (str, bool)) and value in _VALUE_WORDS:
            return _VALUE_WORDS[value]
        return _short(value, 40)

    def condition(self, node: Any) -> str:
        if not node:
            return "always"
        if isinstance(node, dict) and "conditions" in node:
            parts = [self.condition(c) for c in node["conditions"]]
            joined = f" {str(node.get('operator', 'AND')).lower()} ".join(parts)
            return f"({joined})" if len(parts) > 1 else joined
        if not isinstance(node, dict):
            return _short(node)
        op = OPERATORS.get(node.get("comparison", ""))
        label = op.label if op else str(node.get("comparison"))
        value = "" if op is not None and op.operand == "none" else " " + self.value(node.get("value"))
        return f"{self.question(node.get('source_item_code', ''))} {label}{value}"

    def action(self, action: dict[str, Any]) -> str:
        definition = ACTIONS.get(action.get("type", ""))
        label = definition.label.lower() if definition else str(action.get("type"))
        target = action.get("target") or {}
        where = ""
        if target.get("type") == "item":
            where = " " + self.question(target.get("code", ""))
        elif target.get("type") == "section":
            where = " " + _quote(self.sections.get(target.get("code", ""), target.get("code", "")))
        extra = f": {_quote(action['message'])}" if action.get("message") else ""
        return f"{label}{where}{extra}"

    def rule(self, rule: dict[str, Any]) -> str:
        lead = "Always" if not rule.get("condition") else "If " + self.condition(rule["condition"])
        return f"{lead}, " + "; ".join(self.action(a) for a in rule.get("actions", []) if isinstance(a, dict)) + "."


# ---------------------------------------------------------------------------
# The comparison
# ---------------------------------------------------------------------------
def compare_manifests(old: dict[str, Any], new: dict[str, Any]) -> Comparison:
    """What changed from ``old`` to ``new`` (both ``manifest.build_manifest`` outputs)."""
    result = Comparison()
    add = result.changes.append
    old_text, new_text = _Describer(old), _Describer(new)

    # -- sections -------------------------------------------------------------------------------------------
    old_sections = {s["code"]: (i, s) for i, s in enumerate(old["sections"], 1)}
    new_sections = {s["code"]: (i, s) for i, s in enumerate(new["sections"], 1)}
    for code, (_i, s) in new_sections.items():
        if code not in old_sections:
            add(Change("section", "added", code, s["name"], f"Section {_quote(s['name'])} was added ({len(s['items'])} questions).", "structure"))
    for code, (_i, s) in old_sections.items():
        if code not in new_sections:
            add(Change("section", "removed", code, s["name"], f"Section {_quote(s['name'])} was removed ({len(s['items'])} questions).", "structure"))
    for code, (_i, before) in old_sections.items():
        if code not in new_sections:
            continue
        after = new_sections[code][1]
        for name, impact in _SECTION_FIELDS.items():
            if before[name] != after[name]:
                noun = "renamed" if name == "name" else f"{_LABELS[name]} changed"
                add(Change("section", "modified", code, after["name"],
                           f"Section {_quote(before['name'])} {noun}: {_short(before[name])} → {_short(after[name])}.",
                           impact, name, before[name], after[name]))
    for code in _moved(list(old_sections), list(new_sections)):
        add(Change("section", "reordered", code, new_sections[code][1]["name"],
                   f"Section {_quote(new_sections[code][1]['name'])} moved from position {old_sections[code][0]} to {new_sections[code][0]}.",
                   "structure", "position", old_sections[code][0], new_sections[code][0]))

    # -- items -----------------------------------------------------------------------------------------------
    def flatten(manifest: dict[str, Any]) -> dict[str, tuple[str, int, dict[str, Any]]]:
        return {i["code"]: (s["code"], n, i) for s in manifest["sections"] for n, i in enumerate(s["items"], 1)}

    old_items, new_items = flatten(old), flatten(new)
    for code, (scode, _n, item) in new_items.items():
        if code not in old_items:
            add(Change("item", "added", code, item["question"],
                       f"Question {_quote(item['question'])} ({code}) was added to {_quote(new_text.sections[scode])}.", "structure",
                       context=new_text.sections[scode]))
    for code, (scode, _n, item) in old_items.items():
        if code not in new_items:
            add(Change("item", "removed", code, item["question"],
                       f"Question {_quote(item['question'])} ({code}) was removed from {_quote(old_text.sections[scode])}.", "structure",
                       context=old_text.sections[scode]))
    for code, (old_scode, _n, before) in old_items.items():
        if code not in new_items:
            continue
        new_scode, _m, after = new_items[code]
        subject = _quote(after["question"])
        context = new_text.sections.get(new_scode, new_scode)
        if old_scode != new_scode:
            add(Change("item", "modified", code, after["question"],
                       f"Question {subject} moved from {_quote(old_text.sections.get(old_scode, old_scode))} to {_quote(context)}.",
                       "structure", "section", old_text.sections.get(old_scode), context, context))
        for name, impact in _ITEM_FIELDS.items():
            if before[name] != after[name]:
                add(Change("item", "modified", code, after["question"],
                           f"{subject}: {_LABELS[name]} changed: {_short(before[name])} → {_short(after[name])}.", impact, name,
                           before[name], after[name], context))
        for key in sorted(set(before["config"]) | set(after["config"])):
            if before["config"].get(key) != after["config"].get(key) and before["answer_type"] == after["answer_type"]:
                add(Change("item", "modified", code, after["question"],
                           f"{subject}: setting “{key}” changed: {_short(before['config'].get(key))} → {_short(after['config'].get(key))}.",
                           "behavior", f"config.{key}", before["config"].get(key), after["config"].get(key), context))
        if before["answer_type"] != after["answer_type"] and before["config"] != after["config"]:
            add(Change("item", "modified", code, after["question"], f"{subject}: answer configuration was replaced to match the new type.",
                       "behavior", "config", before["config"], after["config"], context))
        # options (matched by value)
        old_options = {o["value"]: (n, o) for n, o in enumerate(before["options"], 1)}
        new_options = {o["value"]: (n, o) for n, o in enumerate(after["options"], 1)}
        for value, (_n, o) in new_options.items():
            if value not in old_options:
                add(Change("option", "added", code, after["question"], f"{subject}: option {_quote(o['label'])} was added.", "behavior",
                           context=context))
        for value, (_n, o) in old_options.items():
            if value not in new_options:
                add(Change("option", "removed", code, after["question"], f"{subject}: option {_quote(o['label'])} was removed.", "behavior",
                           context=context))
        for value, (_n, o_before) in old_options.items():
            if value in new_options:
                o_after = new_options[value][1]
                for name, impact in (("label", "wording"), ("is_active", "behavior")):
                    if o_before[name] != o_after[name]:
                        add(Change("option", "modified", code, after["question"],
                                   f"{subject}: option {_quote(o_before['label'])} {name.replace('_', ' ')} changed: "
                                   f"{_short(o_before[name])} → {_short(o_after[name])}.", impact, f"option.{value}.{name}",
                                   o_before[name], o_after[name], context))
        for value in _moved(list(old_options), list(new_options)):
            add(Change("option", "reordered", code, after["question"],
                       f"{subject}: option {_quote(new_options[value][1]['label'])} moved from position {old_options[value][0]} "
                       f"to {new_options[value][0]}.", "structure", f"option.{value}.position", old_options[value][0], new_options[value][0], context))
    # item order within each section (only items that stayed in the same section)
    for scode in new_sections:
        before_codes = [i["code"] for i in old_sections[scode][1]["items"]] if scode in old_sections else []
        after_codes = [i["code"] for i in new_sections[scode][1]["items"]]
        same = [c for c in after_codes if c in old_items and old_items[c][0] == scode and c in before_codes]
        for code in _moved([c for c in before_codes if c in same], same):
            item = new_items[code][2]
            add(Change("item", "reordered", code, item["question"],
                       f"Question {_quote(item['question'])} moved within {_quote(new_text.sections[scode])}.", "structure",
                       "position", old_items[code][1], new_items[code][1], new_text.sections[scode]))

    # -- rules -----------------------------------------------------------------------------------------------
    old_rules = {r["code"]: (n, r) for n, r in enumerate(old["rules"], 1)}
    new_rules = {r["code"]: (n, r) for n, r in enumerate(new["rules"], 1)}
    for code, (_n, r) in new_rules.items():
        if code not in old_rules:
            add(Change("rule", "added", code, r["name"], f"Rule {code} {_quote(r['name'])} was added: {new_text.rule(r)}", "behavior"))
    for code, (_n, r) in old_rules.items():
        if code not in new_rules:
            add(Change("rule", "removed", code, r["name"], f"Rule {code} {_quote(r['name'])} was removed: {old_text.rule(r)}", "behavior"))
    for code, (_n, before) in old_rules.items():
        if code not in new_rules:
            continue
        after = new_rules[code][1]
        for name, impact in _RULE_FIELDS.items():
            if before[name] == after[name]:
                continue
            if name == "condition":
                text = f"Rule {code} condition changed: {old_text.condition(before['condition'])} → {new_text.condition(after['condition'])}."
            elif name == "actions":
                text = (f"Rule {code} actions changed: {'; '.join(old_text.action(a) for a in before['actions'])} → "
                        f"{'; '.join(new_text.action(a) for a in after['actions'])}.")
            elif name == "is_active":
                text = f"Rule {code} was {'enabled' if after['is_active'] else 'disabled'}."
            else:
                text = f"Rule {code} {_LABELS[name]} changed: {_short(before[name])} → {_short(after[name])}."
            add(Change("rule", "modified", code, after["name"], text, impact, name, before[name], after[name]))
    for code in _moved(list(old_rules), list(new_rules)):
        add(Change("rule", "reordered", code, new_rules[code][1]["name"],
                   f"Rule {code} moved from position {old_rules[code][0]} to {new_rules[code][0]} (changes message order only).",
                   "structure", "position", old_rules[code][0], new_rules[code][0]))

    # -- scoring (extra definition data owned by the scoring app; absent = not configured) ---------------------
    old_scoring, new_scoring = old.get("scoring"), new.get("scoring")
    if old_scoring != new_scoring:
        if old_scoring is None:
            add(Change("scoring", "added", "scoring", "Scoring", "Scoring was configured for this version.", "behavior"))
        elif new_scoring is None:
            add(Change("scoring", "removed", "scoring", "Scoring", "Scoring was removed from this version.", "behavior"))
        else:
            old_set, new_set = old_scoring.get("settings", {}), new_scoring.get("settings", {})
            for key in sorted(set(old_set) | set(new_set)):
                if old_set.get(key) != new_set.get(key):
                    add(Change("scoring", "modified", f"settings.{key}", key.replace("_", " ").capitalize(),
                               f"Scoring {key.replace('_', ' ')} changed: {_short(old_set.get(key))} → {_short(new_set.get(key))}.", "behavior", key,
                               old_set.get(key), new_set.get(key)))
            old_doc, new_doc = old_scoring.get("document") or {}, new_scoring.get("document") or {}
            old_items, new_items_s = old_doc.get("items") or {}, new_doc.get("items") or {}
            for code in sorted(set(old_items) | set(new_items_s)):
                if code not in old_items:
                    add(Change("scoring", "added", f"item.{code}", code, f"Question {code} is now scored.", "behavior"))
                elif code not in new_items_s:
                    add(Change("scoring", "removed", f"item.{code}", code, f"Question {code} is no longer scored.", "behavior"))
                elif old_items[code] != new_items_s[code]:
                    add(Change("scoring", "modified", f"item.{code}", code, f"The scoring rule of question {code} changed.", "behavior", "rule",
                               old_items[code], new_items_s[code]))
            for part, label in (("sections", "Section weights"), ("bands", "Grade bands"), ("critical", "Critical rules")):
                if old_doc.get(part) != new_doc.get(part):
                    add(Change("scoring", "modified", f"document.{part}", label, f"{label} changed.", "behavior", part, old_doc.get(part), new_doc.get(part)))

    # -- automated evaluation rules (extra definition data owned by the evaluation app; absent = not configured) --------------------
    old_eval, new_eval = old.get("evaluation"), new.get("evaluation")
    if old_eval != new_eval:
        if old_eval is None:
            add(Change("evaluation", "added", "evaluation", "Evaluation rules", "Automated evaluation rules were configured for this version.", "behavior"))
        elif new_eval is None:
            add(Change("evaluation", "removed", "evaluation", "Evaluation rules", "Automated evaluation rules were removed from this version.", "behavior"))
        else:
            if old_eval.get("enabled") != new_eval.get("enabled"):
                add(Change("evaluation", "modified", "enabled", "Enabled", f"Automated evaluation was {'switched on' if new_eval.get('enabled') else 'switched off'}.", "behavior",
                           "enabled", old_eval.get("enabled"), new_eval.get("enabled")))
            old_r = {r.get("code"): r for r in old_eval.get("rules") or []}
            new_r = {r.get("code"): r for r in new_eval.get("rules") or []}
            for code in sorted(set(old_r) | set(new_r), key=str):
                if code not in old_r:
                    add(Change("evaluation", "added", f"rule.{code}", str(code), f"Evaluation rule {code} {_quote(str(new_r[code].get('name', '')))} was added.", "behavior"))
                elif code not in new_r:
                    add(Change("evaluation", "removed", f"rule.{code}", str(code), f"Evaluation rule {code} {_quote(str(old_r[code].get('name', '')))} was removed.", "behavior"))
                elif old_r[code] != new_r[code]:
                    changed = sorted(key for key in set(old_r[code]) | set(new_r[code]) if old_r[code].get(key) != new_r[code].get(key))
                    add(Change("evaluation", "modified", f"rule.{code}", str(code), f"Evaluation rule {code} changed ({', '.join(changed)}).", "behavior", "rule",
                               old_r[code], new_r[code]))
    return result
