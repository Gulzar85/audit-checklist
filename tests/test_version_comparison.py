"""Version comparison: a pure diff of two manifests, matched by stable codes."""

from __future__ import annotations

import copy

import pytest

from apps.checklists.comparison import compare_manifests

# ---------------------------------------------------------------------------
# Hand-built manifests (no database): the comparison is a pure function.
# ---------------------------------------------------------------------------


def item(code, question, answer_type="yes_no", *, config=None, options=None, required=True, active=True, instructions="", help_text=""):
    return {
        "code": code, "question": question, "description": "", "help_text": help_text, "instructions": instructions,
        "answer_type": answer_type, "config": config or {}, "metadata": {}, "is_required": required, "is_active": active,
        "options": options if options is not None else (
            [{"value": "yes", "label": "Yes", "is_active": True}, {"value": "no", "label": "No", "is_active": True}] if answer_type == "yes_no" else []),
    }


def section(code, name, *items, required=True, description="", instructions=""):
    return {"code": code, "name": name, "description": description, "instructions": instructions, "is_required": required, "items": list(items)}


def rule(code, name, condition, actions, *, active=True):
    return {"code": code, "name": name, "description": "", "condition": condition, "actions": actions, "is_active": active}


def manifest(sections, rules=()):
    return {"schema": 1, "sections": list(sections), "rules": list(rules)}


def cond(source, comparison, value=None):
    out = {"source_item_code": source, "comparison": comparison}
    if value is not None:
        out["value"] = value
    return {"operator": "AND", "conditions": [out]}


def act(kind, target=None, **params):
    out = {"type": kind, **params}
    if target:
        out["target"] = {"type": "item", "code": target}
    return out


def base():
    return manifest(
        [
            section("S-001", "Fire safety", item("FIRE", "Is the fire exit accessible?"), item("NOTE", "Describe the issue.", "long_text", required=False)),
            section("S-002", "Food storage", item("TEMP", "Refrigerator temperature", "numeric", config={"min": 0, "max": 10, "decimals": 1, "unit": "°C"})),
            section("S-003", "General", item("LEGACY", "Legacy equipment question")),
        ],
        [rule("R-001", "Blocked exit", cond("FIRE", "EQUALS", "no"), [act("SHOW_ITEM", "NOTE")])],
    )


def kinds(result, category=None):
    return sorted((c.category, c.kind, c.code, c.field) for c in result.changes if category in (None, c.category))


class TestIdentity:
    def test_identical_manifests_have_no_changes(self):
        result = compare_manifests(base(), copy.deepcopy(base()))
        assert result.identical and result.counts()["total"] == 0 and result.as_dict()["changes"] == []

    def test_primary_keys_and_sort_order_numbers_are_not_part_of_the_manifest(self):
        # The comparison sees only meaning: two equal manifests are equal whatever produced them.
        assert compare_manifests(base(), base()).identical


class TestSections:
    def test_added_removed_and_renamed(self):
        new = base()
        new["sections"][2] = section("S-004", "Drive-through", item("DT", "Is the lane clear?"))
        new["sections"][0]["name"] = "Fire and life safety"
        result = compare_manifests(base(), new)
        assert ("section", "added", "S-004", "") in kinds(result) and ("section", "removed", "S-003", "") in kinds(result)
        assert ("section", "modified", "S-001", "name") in kinds(result)
        renamed = next(c for c in result.changes if c.field == "name")
        assert "renamed" in renamed.summary and renamed.impact == "wording"

    def test_reordering_reports_only_the_section_that_moved(self):
        new = base()
        new["sections"] = [new["sections"][2], new["sections"][0], new["sections"][1]]  # S-003 moved to the front
        moved = [c for c in compare_manifests(base(), new).changes if c.kind == "reordered"]
        assert [c.code for c in moved] == ["S-003"] and "position 3 to 1" in moved[0].summary

    def test_swap_reports_both_ends_without_reporting_the_middle(self):
        new = base()
        new["sections"] = [new["sections"][2], new["sections"][1], new["sections"][0]]
        assert sorted(c.code for c in compare_manifests(base(), new).changes if c.kind == "reordered") in (["S-001", "S-002"], ["S-001", "S-003"], ["S-002", "S-003"])


class TestItems:
    def test_added_and_removed_questions(self):
        new = base()
        new["sections"][1]["items"].append(item("HAND", "Is the handwashing station operational?", "yes_no_na"))
        del new["sections"][2]["items"][0]
        result = compare_manifests(base(), new)
        assert ("item", "added", "HAND", "") in kinds(result) and ("item", "removed", "LEGACY", "") in kinds(result)
        assert "handwashing" in next(c for c in result.changes if c.code == "HAND").summary

    def test_changed_question_text_is_wording(self):
        new = base()
        new["sections"][0]["items"][0]["question"] = "Is every fire exit clear of obstructions?"
        change = next(c for c in compare_manifests(base(), new).changes if c.field == "question")
        assert change.kind == "modified" and change.impact == "wording" and change.after.startswith("Is every")

    def test_changed_instructions_and_help_text(self):
        new = base()
        new["sections"][0]["items"][0]["instructions"] = "Walk the full route."
        new["sections"][0]["items"][0]["help_text"] = "Includes the rear door."
        assert {"instructions", "help_text"} <= {c.field for c in compare_manifests(base(), new).changes}

    def test_same_code_with_a_different_answer_type_is_modified_not_unchanged(self):
        new = base()
        new["sections"][0]["items"][0] = item("FIRE", "Is the fire exit accessible?", "yes_no_na", options=[
            {"value": "yes", "label": "Yes", "is_active": True}, {"value": "no", "label": "No", "is_active": True}, {"value": "na", "label": "N/A", "is_active": True}])
        result = compare_manifests(base(), new)
        change = next(c for c in result.changes if c.field == "answer_type")
        assert change.kind == "modified" and change.impact == "behavior" and (change.before, change.after) == ("yes_no", "yes_no_na")
        assert any(c.category == "option" and c.kind == "added" for c in result.changes)  # and the new "N/A" option is reported

    def test_changed_numeric_range_is_reported_per_setting(self):
        new = base()
        new["sections"][1]["items"][0]["config"]["max"] = 8
        result = compare_manifests(base(), new)
        change = next(c for c in result.changes if c.field == "config.max")
        assert (change.before, change.after, change.impact) == (10, 8, "behavior") and "10 → 8" in change.summary
        assert [c.field for c in result.changes] == ["config.max"]

    def test_requiredness_and_active_flags(self):
        new = base()
        new["sections"][0]["items"][1]["is_required"] = True
        new["sections"][0]["items"][0]["is_active"] = False
        assert {"is_required", "is_active"} == {c.field for c in compare_manifests(base(), new).changes}

    def test_an_item_moved_to_another_section(self):
        new = base()
        new["sections"][1]["items"].append(new["sections"][0]["items"].pop(1))
        change = next(c for c in compare_manifests(base(), new).changes if c.field == "section")
        assert change.code == "NOTE" and "moved from" in change.summary

    def test_reordering_questions_inside_a_section(self):
        old = base()
        old["sections"][0]["items"].append(item("EXTRA", "Extra question"))
        new = copy.deepcopy(old)
        new["sections"][0]["items"] = [new["sections"][0]["items"][2]] + new["sections"][0]["items"][:2]
        moved = [c for c in compare_manifests(old, new).changes if c.kind == "reordered"]
        assert [c.code for c in moved] == ["EXTRA"]


class TestOptions:
    def make(self, labels):
        return manifest([section("S-001", "A", item("CHOICE", "Pick one", "single_choice", options=[
            {"value": v, "label": label, "is_active": True} for v, label in labels]))])

    def test_added_removed_relabelled_and_reordered(self):
        old = self.make([("good", "Good"), ("fair", "Fair"), ("poor", "Poor")])
        new = self.make([("poor", "Poor"), ("good", "Very good"), ("great", "Great")])
        result = compare_manifests(old, new)
        got = {(c.category, c.kind, c.field.split(".")[-1] if c.field else "") for c in result.changes}
        assert ("option", "added", "") in got and ("option", "removed", "") in got  # great added, fair removed
        assert ("option", "modified", "label") in got and ("option", "reordered", "position") in got

    def test_deactivating_an_option_is_behavioural(self):
        old = self.make([("a", "A"), ("b", "B")])
        new = copy.deepcopy(old)
        new["sections"][0]["items"][0]["options"][1]["is_active"] = False
        change = next(c for c in compare_manifests(old, new).changes if c.field.endswith("is_active"))
        assert change.impact == "behavior"


class TestRules:
    def test_added_removed_and_changed_rules(self):
        new = base()
        new["rules"] = [
            rule("R-001", "Blocked exit", cond("FIRE", "EQUALS", "no"), [act("SHOW_ITEM", "NOTE"), act("REQUIRE_COMMENT", "FIRE")]),
            rule("R-002", "Temperature warning", cond("TEMP", "GREATER_THAN", "8"), [act("DISPLAY_MESSAGE", message="Too warm", severity="warning")]),
        ]
        result = compare_manifests(base(), new)
        assert ("rule", "added", "R-002", "") in kinds(result) and ("rule", "modified", "R-001", "actions") in kinds(result)
        added = next(c for c in result.changes if c.kind == "added" and c.category == "rule")
        assert "Temperature" in added.summary and "is greater than 8" in added.summary and "Too warm" in added.summary
        changed = next(c for c in result.changes if c.field == "actions")
        assert "require a comment" in changed.summary.lower()

    def test_a_changed_condition_reads_in_words(self):
        new = base()
        new["rules"][0]["condition"] = cond("FIRE", "EQUALS", "yes")
        change = next(c for c in compare_manifests(base(), new).changes if c.field == "condition")
        assert "equals No" in change.summary and "equals Yes" in change.summary and change.impact == "behavior"

    def test_disabling_and_removing(self):
        new = base()
        new["rules"][0]["is_active"] = False
        assert next(c for c in compare_manifests(base(), new).changes if c.field == "is_active").summary.endswith("was disabled.")
        new["rules"] = []
        assert ("rule", "removed", "R-001", "") in kinds(compare_manifests(base(), new))


class TestFilteringAndOutput:
    def build(self):
        new = base()
        new["sections"][1]["items"][0]["config"]["max"] = 8
        new["sections"][0]["items"][0]["question"] = "Is every fire exit clear?"
        new["rules"] = []
        return compare_manifests(base(), new)

    def test_counts_and_filters(self):
        result = self.build()
        counts = result.counts()
        assert counts["total"] == 3 and counts["by_category"]["rule"] == 1 and counts["by_impact"]["behavior"] == 2
        assert len(result.filtered(category="item")) == 2 and len(result.filtered(impact="wording")) == 1 and len(result.filtered(kind="removed")) == 1
        assert result.filtered(category="item", impact="behavior")[0].field == "config.max"

    def test_as_dict_is_json_safe(self):
        import json

        payload = json.loads(json.dumps(self.build().as_dict()))
        assert payload["identical"] is False and payload["changes"][0]["category"] in ("item", "rule")

    @pytest.mark.parametrize("bad", [{"schema": 1, "sections": [], "rules": []}])
    def test_empty_manifests_compare_cleanly(self, bad):
        assert compare_manifests(bad, bad).identical
