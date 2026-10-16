"""The evaluator against the language-neutral vectors in ``docs/rule-conformance-vectors.json``.

The vectors are the contract a second implementation (the PWA's offline evaluator) must meet; the Python evaluator is the
reference.  Editing the evaluator's behaviour means editing the vectors on purpose, so a silent drift cannot happen.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from apps.checklists.answer_types import registry
from apps.checklists.rules import RuleSpec, VersionSnapshot, compile_rules, evaluate
from apps.checklists.rules.snapshot import ItemSpec, OptionSpec, SectionSpec

VECTORS = json.loads((Path(__file__).resolve().parent.parent / "docs" / "rule-conformance-vectors.json").read_text(encoding="utf-8"))


def build(vector) -> VersionSnapshot:
    sections = {code: SectionSpec(f"sid-{code}", code, f"Section {code}", n) for n, code in enumerate(vector["sections"], 1)}
    items = {}
    for n, spec in enumerate(vector["items"], 1):
        definition = registry.get(spec["answer_type"])
        items[spec["code"]] = ItemSpec(
            id=f"id-{spec['code']}", code=spec["code"], section_code=spec.get("section", vector["sections"][0]), question=f"Question {spec['code']}",
            answer_type=spec["answer_type"], config=definition.clean_config(spec.get("config") or {}), required=spec.get("required", True),
            active=True, sort_order=n,
            options=tuple(OptionSpec(f"o-{spec['code']}-{v}", v, v.title(), i, True) for i, v in enumerate(spec.get("options", []), 1)),
        )
    rules = tuple(
        RuleSpec(code=r["code"], name=r["code"], condition=r.get("condition"), actions=r["actions"], is_active=r.get("enabled", True), sort_order=n)
        for n, r in enumerate(vector["rules"], 1)
    )
    return VersionSnapshot("v", "org", sections, items, rules)


CASES = [(v["name"], v, c) for v in VECTORS["vectors"] for c in v["cases"]]


@pytest.mark.parametrize("name,vector,case", CASES, ids=[f"{v['name'][:50]}#{i}" for i, (_n, v, _c) in enumerate(CASES)])
def test_vector(name, vector, case):
    snapshot = build(vector)
    compiled = compile_rules(snapshot)
    assert not [i for i in compiled.issues if i.level == "error"], "vectors must be valid rule sets"
    result = evaluate(compiled, case["answers"], case.get("not_applicable", []))
    expect = case["expect"]
    for code, visible in expect.get("visible", {}).items():
        assert result.items[code].visible is visible, f"visible {code}"
    for code, required in expect.get("required", {}).items():
        assert result.items[code].required is required, f"required {code}"
    for code, visible in expect.get("sections", {}).items():
        assert result.sections[code]["visible"] is visible, f"section {code}"
    if "messages" in expect:
        assert [m["message"] for m in result.messages] == expect["messages"]
    if "comment_required" in expect:
        assert sorted(c for c, r in result.items.items() if r.comment_required) == sorted(expect["comment_required"])
    for code, prompts in expect.get("comment_prompts", {}).items():
        assert result.items[code].comment_prompts == prompts
    for code, evidence in expect.get("evidence", {}).items():
        assert result.items[code].evidence == evidence, f"evidence {code}"
    for code, status in expect.get("answer_status", {}).items():
        assert result.items[code].answer.status == status, f"status {code}"
    for code, options in expect.get("options", {}).items():
        assert result.items[code].options == options
    for code, constraints in expect.get("constraints", {}).items():
        assert result.items[code].constraints == constraints
    if "unanswered_required" in expect:
        assert result.completion["unanswered_required"] == expect["unanswered_required"]
    if "invalid" in expect:
        assert result.completion["invalid"] == expect["invalid"]
    if "complete" in expect:
        assert result.completion["complete"] is expect["complete"]


def test_every_documented_semantic_has_a_vector():
    names = " ".join(v["name"] for v in VECTORS["vectors"]).lower()
    for topic in ("unknown", "is_empty", "nested", "between", "case-insensitive", "multiple choice", "section visibility", "conflicts",
                  "comment and evidence", "option availability", "n/a"):
        assert topic in names, topic
