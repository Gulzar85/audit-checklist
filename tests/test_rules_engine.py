"""The rules engine, tested in isolation (no database): operators, three-valued logic, actions, validation, cycles."""

from __future__ import annotations

import random
from decimal import Decimal

import pytest

from apps.checklists.answer_types import registry
from apps.checklists.rules import (
    RuleSpec,
    Tri,
    VersionSnapshot,
    compile_rules,
    describe_rule,
    evaluate,
    operators_for,
    validate_rule_set,
)
from apps.checklists.rules import limits as rule_limits
from apps.checklists.rules.operators import OPERATORS, evaluate_comparison
from apps.checklists.rules.snapshot import ItemSpec, OptionSpec, SectionSpec
from apps.checklists.rules.state import ANSWERED, EMPTY, HIDDEN, INVALID, NOT_APPLICABLE, AnswerState
from apps.checklists.rules.tri import tri_and, tri_or


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------
def item(code, answer_type="yes_no", *, section="S1", config=None, required=True, options=(), active=True, question=None, sort=0):
    definition = registry.get(answer_type)
    return ItemSpec(
        id=f"id-{code}", code=code, section_code=section, question=question or f"Question {code}", answer_type=answer_type,
        config=definition.clean_config(config or ({"rows": ["r"], "columns": ["c"]} if answer_type == "matrix" else {})),
        required=required, active=active, sort_order=sort,
        options=tuple(OptionSpec(f"o-{code}-{v}", v, v.title(), i, True) for i, v in enumerate(options, 1)),
    )


def snap(items, rules=(), sections=("S1",)):
    items = list(items)
    for n, i in enumerate(items):
        if i.sort_order == 0:
            items[n] = ItemSpec(**{**i.__dict__, "sort_order": n + 1})
    return VersionSnapshot(
        "v1", "org1",
        {s: SectionSpec(f"sid-{s}", s, f"Section {s}", n) for n, s in enumerate(sections, 1)},
        {i.code: i for i in items},
        tuple(rules),
    )


def rule(code, condition, actions, *, active=True, order=0):
    return RuleSpec(code=code, name=f"Rule {code}", condition=condition, actions=actions, is_active=active, sort_order=order)


def cond(source, comparison, value=None):
    out = {"source_item_code": source, "comparison": comparison}
    if value is not None:
        out["value"] = value
    return out


def group(op, *conditions):
    return {"operator": op, "conditions": list(conditions)}


def act(kind, target=None, target_type="item", **params):
    out = {"type": kind, **params}
    if target:
        out["target"] = {"type": target_type, "code": target}
    return out


def run(snapshot, answers=None, na=()):
    return evaluate(compile_rules(snapshot), answers or {}, na)


def issues(snapshot):
    return validate_rule_set(snapshot)


def codes(report, level="error"):
    return [i.code for i in report.issues if i.level == level]


# ---------------------------------------------------------------------------
class TestOperatorRegistry:
    def test_operators_follow_the_answer_types_family(self):
        by_type = {d.code: {o.code for o in operators_for(d.code)} for d in registry.all()}
        assert by_type["boolean"] == {"EQUALS", "NOT_EQUALS", "IS_EMPTY", "IS_NOT_EMPTY"}
        assert by_type["yes_no"] == by_type["yes_no_na"] == by_type["boolean"]
        assert "GREATER_THAN" not in by_type["boolean"] and "BETWEEN" not in by_type["yes_no"]
        assert {"GREATER_THAN", "GREATER_THAN_OR_EQUAL", "LESS_THAN", "LESS_THAN_OR_EQUAL", "BETWEEN", "NOT_BETWEEN"} <= by_type["numeric"]
        assert by_type["score"] == by_type["numeric"] == by_type["date"] == by_type["datetime"]
        assert {"CONTAINS", "DOES_NOT_CONTAIN", "STARTS_WITH", "ENDS_WITH"} <= by_type["text"] and by_type["text"] == by_type["long_text"]
        assert {"SELECTED", "NOT_SELECTED", "CONTAINS_ANY"} <= by_type["single_choice"] and "CONTAINS_ALL" not in by_type["single_choice"]
        assert {"SELECTED", "NOT_SELECTED", "CONTAINS_ANY", "CONTAINS_ALL"} <= by_type["multiple_choice"]
        for presence_only in ("photo", "file", "signature", "matrix"):
            assert by_type[presence_only] == {"IS_EMPTY", "IS_NOT_EMPTY"}
        assert "GREATER_THAN" not in by_type["text"] and "CONTAINS" not in by_type["numeric"]

    def test_every_registered_type_declares_a_known_family(self):
        for definition in registry.all():
            assert definition.comparison_family in {"equality", "choice", "multi_choice", "ordered", "text", "presence"}

    def test_every_operator_is_available_somewhere(self):
        available = {o.code for d in registry.all() for o in operators_for(d.code)}
        assert available == set(OPERATORS)

    def test_unknown_type_has_no_operators(self):
        assert operators_for("telepathy") == []


# ---------------------------------------------------------------------------
class TestThreeValuedLogic:
    T, F, U = Tri.TRUE, Tri.FALSE, Tri.UNKNOWN

    def test_and(self):
        assert tri_and([self.T, self.T]) is self.T
        assert tri_and([self.T, self.F]) is self.F
        assert tri_and([self.U, self.F]) is self.F  # a definite FALSE decides
        assert tri_and([self.T, self.U]) is self.U
        assert tri_and([]) is self.T

    def test_or(self):
        assert tri_or([self.F, self.F]) is self.F
        assert tri_or([self.F, self.T]) is self.T
        assert tri_or([self.U, self.T]) is self.T  # a definite TRUE decides
        assert tri_or([self.F, self.U]) is self.U
        assert tri_or([]) is self.F

    def test_unknown_never_triggers_actions(self):
        s = snap([item("A"), item("B")], [rule("R1", cond("A", "EQUALS", "no"), [act("HIDE_ITEM", "B")])])
        assert run(s, {}).items["B"].visible is True  # A unanswered: UNKNOWN, no action
        assert run(s, {"A": "no"}).items["B"].visible is False
        assert run(s, {"A": "yes"}).items["B"].visible is True


# ---------------------------------------------------------------------------
class TestComparisons:
    def cmp(self, answer_type, comparison, operand, answer, config=None, options=()):
        i = item("Q", answer_type, config=config, options=options)
        s = snap([i, item("T")], [rule("R", cond("Q", comparison, operand), [act("HIDE_ITEM", "T")])])
        report = issues(s)
        assert report.ok, report.errors
        result = run(s, {"Q": answer})
        return {True: Tri.TRUE, False: Tri.FALSE}[result.items["T"].visible is False] if result.rules[0]["matched"] != "unknown" else Tri.UNKNOWN

    @pytest.mark.parametrize("comparison,operand,answer,expected", [
        ("EQUALS", "no", "no", Tri.TRUE), ("EQUALS", "no", "yes", Tri.FALSE), ("EQUALS", "no", "na", Tri.FALSE),
        ("NOT_EQUALS", "no", "yes", Tri.TRUE), ("NOT_EQUALS", "no", "na", Tri.TRUE), ("NOT_EQUALS", "no", "no", Tri.FALSE),
    ])
    def test_yes_no_na_keeps_na_distinct_from_no(self, comparison, operand, answer, expected):
        assert self.cmp("yes_no_na", comparison, operand, answer) is expected

    def test_boolean(self):
        assert self.cmp("boolean", "EQUALS", True, True) is Tri.TRUE
        assert self.cmp("boolean", "EQUALS", True, False) is Tri.FALSE
        assert self.cmp("boolean", "EQUALS", False, False) is Tri.TRUE  # False is an answer, not "missing"

    @pytest.mark.parametrize("comparison,operand,answer,expected", [
        ("GREATER_THAN", 5, "5.01", Tri.TRUE), ("GREATER_THAN", 5, "5", Tri.FALSE),
        ("GREATER_THAN_OR_EQUAL", 5, "5", Tri.TRUE), ("LESS_THAN", 5, "4.99", Tri.TRUE), ("LESS_THAN", 5, "5", Tri.FALSE),
        ("LESS_THAN_OR_EQUAL", 5, "5", Tri.TRUE), ("EQUALS", "4.5", "4.50", Tri.TRUE), ("NOT_EQUALS", "4.5", "4.6", Tri.TRUE),
        ("BETWEEN", [1, 5], "1", Tri.TRUE), ("BETWEEN", [1, 5], "5", Tri.TRUE), ("BETWEEN", [1, 5], "5.01", Tri.FALSE),
        ("NOT_BETWEEN", [1, 5], "5.01", Tri.TRUE), ("NOT_BETWEEN", [1, 5], "3", Tri.FALSE),
    ])
    def test_numeric_uses_exact_decimals(self, comparison, operand, answer, expected):
        assert self.cmp("numeric", comparison, operand, answer) is expected

    def test_decimal_precision_is_exact_not_float(self):
        s = snap([item("Q", "numeric", config={"decimals": 1}), item("T")],
                 [rule("R", cond("Q", "EQUALS", "0.3"), [act("HIDE_ITEM", "T")])])
        assert run(s, {"Q": Decimal("0.1") + Decimal("0.2")}).items["T"].visible is False

    def test_score(self):
        assert self.cmp("score", "GREATER_THAN_OR_EQUAL", 4, 4, {"min": 1, "max": 5}) is Tri.TRUE
        assert self.cmp("score", "LESS_THAN", 3, 4, {"min": 1, "max": 5}) is Tri.FALSE
        assert self.cmp("score", "BETWEEN", [2, 3], 3, {"min": 1, "max": 5}) is Tri.TRUE

    @pytest.mark.parametrize("comparison,operand,answer,expected", [
        ("CONTAINS", "leak", "Water LEAK found", Tri.TRUE), ("CONTAINS", "leak", "all fine", Tri.FALSE),
        ("DOES_NOT_CONTAIN", "leak", "all fine", Tri.TRUE), ("STARTS_WITH", "water", "Water leak", Tri.TRUE),
        ("ENDS_WITH", "leak", "Water leak", Tri.TRUE), ("ENDS_WITH", "leak", "leaks", Tri.FALSE),
        ("EQUALS", "OK", "ok", Tri.TRUE), ("NOT_EQUALS", "ok", "fine", Tri.TRUE),
    ])
    def test_text_comparisons_are_case_insensitive(self, comparison, operand, answer, expected):
        assert self.cmp("text", comparison, operand, answer) is expected

    def test_text_normalization_is_unicode_aware(self):
        assert self.cmp("text", "EQUALS", "Straße", "STRASSE") is Tri.TRUE  # casefold, not lower()
        assert self.cmp("text", "CONTAINS", "ﬁre", "a fire exit") is Tri.TRUE  # NFKC: the ligature ﬁ -> fi

    def test_single_choice(self):
        o = ("excellent", "good", "damaged")
        assert self.cmp("single_choice", "EQUALS", "damaged", "damaged", options=o) is Tri.TRUE
        assert self.cmp("single_choice", "SELECTED", "damaged", "good", options=o) is Tri.FALSE
        assert self.cmp("single_choice", "NOT_SELECTED", "damaged", "good", options=o) is Tri.TRUE
        assert self.cmp("single_choice", "CONTAINS_ANY", ["good", "damaged"], "good", options=o) is Tri.TRUE
        assert self.cmp("single_choice", "CONTAINS_ANY", ["excellent"], "good", options=o) is Tri.FALSE

    def test_multiple_choice(self):
        o = ("cleaning", "safety", "training")
        assert self.cmp("multiple_choice", "SELECTED", "safety", ["safety", "cleaning"], options=o) is Tri.TRUE
        assert self.cmp("multiple_choice", "NOT_SELECTED", "training", ["safety"], options=o) is Tri.TRUE
        assert self.cmp("multiple_choice", "CONTAINS_ANY", ["training", "safety"], ["safety"], options=o) is Tri.TRUE
        assert self.cmp("multiple_choice", "CONTAINS_ALL", ["training", "safety"], ["safety"], options=o) is Tri.FALSE
        assert self.cmp("multiple_choice", "CONTAINS_ALL", ["safety"], ["safety", "cleaning"], options=o) is Tri.TRUE

    def test_dates_and_datetimes(self):
        assert self.cmp("date", "GREATER_THAN", "2026-01-01", "2026-06-01") is Tri.TRUE
        assert self.cmp("date", "LESS_THAN_OR_EQUAL", "2026-06-01", "2026-06-01") is Tri.TRUE
        assert self.cmp("date", "BETWEEN", ["2026-01-01", "2026-12-31"], "2027-01-01") is Tri.FALSE
        assert self.cmp("datetime", "LESS_THAN", "2026-06-01T12:00:00+00:00", "2026-06-01T09:00:00+00:00") is Tri.TRUE
        # +05:00 midnight is 19:00 UTC the previous day: timezone-consistent comparison.
        assert self.cmp("datetime", "LESS_THAN", "2026-06-01T00:00:00+00:00", "2026-06-01T03:00:00+05:00") is Tri.TRUE

    def test_naive_datetime_operand_uses_the_project_timezone(self, settings):
        from django.utils import timezone

        settings.TIME_ZONE = "Asia/Karachi"
        timezone.activate("Asia/Karachi")
        try:
            assert self.cmp("datetime", "EQUALS", "2026-06-01T10:00:00", "2026-06-01T05:00:00+00:00") is Tri.TRUE
        finally:
            timezone.deactivate()

    def test_a_threshold_outside_the_items_own_range_is_a_valid_rule(self):
        s = snap([item("Q", "numeric", config={"min": 0, "max": 20}), item("T")],
                 [rule("R", cond("Q", "GREATER_THAN", 25), [act("HIDE_ITEM", "T")])])
        assert issues(s).ok  # parse_operand ignores the item's range; the rule simply never matches


# ---------------------------------------------------------------------------
class TestMissingValues:
    @pytest.mark.parametrize("comparison,operand", [("EQUALS", "no"), ("NOT_EQUALS", "no")])
    def test_unanswered_is_not_no(self, comparison, operand):
        op = OPERATORS[comparison]
        for status in (EMPTY, NOT_APPLICABLE, INVALID, HIDDEN):
            assert evaluate_comparison(op, "equality", AnswerState(status), operand) is Tri.UNKNOWN

    def test_unanswered_numeric_is_not_zero(self):
        s = snap([item("Q", "numeric"), item("T")], [rule("R", cond("Q", "LESS_THAN", 5), [act("HIDE_ITEM", "T")])])
        assert run(s, {}).rules[0]["matched"] == "unknown"  # would be TRUE if blank were read as 0
        assert run(s, {"Q": ""}).rules[0]["matched"] == "unknown"
        assert run(s, {"Q": 0}).rules[0]["matched"] == "true"  # a real zero is an answer

    def test_is_empty_and_is_not_empty_table(self):
        e, ne = OPERATORS["IS_EMPTY"], OPERATORS["IS_NOT_EMPTY"]
        expectations = {
            EMPTY: (Tri.TRUE, Tri.FALSE), ANSWERED: (Tri.FALSE, Tri.TRUE), NOT_APPLICABLE: (Tri.FALSE, Tri.FALSE),
            INVALID: (Tri.UNKNOWN, Tri.UNKNOWN), HIDDEN: (Tri.UNKNOWN, Tri.UNKNOWN),
        }
        for status, (empty_result, not_empty_result) in expectations.items():
            state = AnswerState(status, "x" if status == ANSWERED else None)
            assert evaluate_comparison(e, "text", state, None) is empty_result, status
            assert evaluate_comparison(ne, "text", state, None) is not_empty_result, status

    def test_invalid_source_answer_does_not_match(self):
        s = snap([item("Q", "score", config={"min": 1, "max": 5}), item("T")],
                 [rule("R", cond("Q", "GREATER_THAN", 2), [act("HIDE_ITEM", "T")])])
        result = run(s, {"Q": 99})  # outside 1-5 -> invalid by Phase 5
        assert result.items["Q"].answer.status == INVALID and result.rules[0]["matched"] == "unknown"
        assert result.items["T"].visible is True

    def test_na_flag_is_distinct_from_no(self):
        s = snap([item("Q", "yes_no_na", required=False), item("T")],
                 [rule("R", cond("Q", "EQUALS", "no"), [act("HIDE_ITEM", "T")]),
                  rule("R2", cond("Q", "IS_EMPTY"), [act("DISPLAY_MESSAGE", message="empty")])])
        result = run(s, {}, na=["Q"])
        assert result.items["Q"].answer.status == NOT_APPLICABLE
        assert [r["matched"] for r in result.rules] == ["unknown", "false"]  # N/A: not "no", and not "empty" either


# ---------------------------------------------------------------------------
class TestGroups:
    def two(self, op, a_answer, b_answer):
        s = snap([item("A"), item("B", "numeric"), item("T")], [rule("R", group(op, cond("A", "EQUALS", "yes"), cond("B", "GREATER_THAN", 5)), [act("SHOW_ITEM", "T")])])
        answers = {k: v for k, v in (("A", a_answer), ("B", b_answer)) if v is not None}
        return run(s, answers)

    def test_scenario_d_and(self):  # Q-A is Yes AND Q-B > 5
        assert self.two("AND", "yes", 6).items["T"].visible is True
        assert self.two("AND", "yes", 5).items["T"].visible is False  # one condition false
        assert self.two("AND", "no", 9).items["T"].visible is False
        assert self.two("AND", "yes", None).rules[0]["matched"] == "unknown"  # waiting for B
        assert self.two("AND", "no", None).rules[0]["matched"] == "false"  # A already decides

    def test_or(self):
        assert self.two("OR", "yes", None).rules[0]["matched"] == "true"
        assert self.two("OR", "no", 9).items["T"].visible is True
        assert self.two("OR", "no", 1).rules[0]["matched"] == "false"
        assert self.two("OR", "no", None).rules[0]["matched"] == "unknown"

    def test_nested_groups(self):
        s = snap([item("A"), item("B"), item("C", "numeric"), item("T")], [rule("R", group(
            "AND", cond("A", "EQUALS", "yes"), group("OR", cond("B", "EQUALS", "yes"), cond("C", "LESS_THAN", 3))), [act("SHOW_ITEM", "T")])])
        assert run(s, {"A": "yes", "B": "no", "C": 2}).items["T"].visible is True
        assert run(s, {"A": "yes", "B": "no", "C": 4}).items["T"].visible is False
        assert run(s, {"A": "no", "B": "yes", "C": 2}).items["T"].visible is False
        assert run(s, {"A": "yes", "B": "yes"}).items["T"].visible is True  # OR decided by B; C unknown does not matter

    def test_empty_condition_means_always(self):
        s = snap([item("A")], [rule("R", None, [act("DISPLAY_MESSAGE", message="Always shown")])])
        assert run(s).messages[0]["message"] == "Always shown"
        s = snap([item("A")], [rule("R", {"operator": "AND", "conditions": []}, [act("DISPLAY_MESSAGE", message="x")])])
        assert issues(s).ok and run(s).rules[0]["matched"] == "true"

    def test_bare_condition_is_wrapped(self):
        s = snap([item("A"), item("T")], [rule("R", cond("A", "EQUALS", "no"), [act("HIDE_ITEM", "T")])])
        compiled = compile_rules(s)
        assert compiled.rules[0].stored_condition == {"operator": "AND", "conditions": [{"source_item_code": "A", "comparison": "EQUALS", "value": "no"}]}

    def test_depth_limit_is_enforced_and_configurable(self, settings):
        def nested(depth):
            node = cond("A", "EQUALS", "yes")
            for _ in range(depth):
                node = group("AND", node)
            return node

        s = snap([item("A"), item("T")], [rule("R", nested(3), [act("HIDE_ITEM", "T")])])
        assert issues(s).ok  # depth 3 = default limit
        s = snap([item("A"), item("T")], [rule("R", nested(4), [act("HIDE_ITEM", "T")])])
        assert "too_deep" in codes(issues(s))
        settings.CHECKLIST_RULE_LIMITS = {"max_group_depth": 5}
        assert issues(s).ok

    def test_condition_count_limit(self, settings):
        settings.CHECKLIST_RULE_LIMITS = {"max_conditions_per_rule": 3}
        s = snap([item("A"), item("T")], [rule("R", group("OR", *[cond("A", "EQUALS", "yes")] * 4), [act("HIDE_ITEM", "T")])])
        assert "too_many" in codes(issues(s))


# ---------------------------------------------------------------------------
class TestScenarios:
    def fire(self):
        return snap(
            [item("FIRE_EXIT"), item("FOLLOWUP", "long_text", required=False), item("MANAGER_COMMENT", "long_text", required=False),
             item("PHOTO", "photo", required=False)],
            [
                rule("R-1", cond("FIRE_EXIT", "EQUALS", "no"), [
                    act("SHOW_ITEM", "FOLLOWUP"),
                    act("REQUIRE_ITEM", "MANAGER_COMMENT"),
                    act("REQUIRE_COMMENT", "FIRE_EXIT", prompt="Describe the obstruction"),
                    act("REQUIRE_EVIDENCE", "FIRE_EXIT", kinds=["photo"]),
                    act("DISPLAY_MESSAGE", "FIRE_EXIT", severity="warning", message="A blocked fire exit must be reported immediately."),
                ], order=1),
            ],
        )

    def test_scenario_a_no(self):
        r = run(self.fire(), {"FIRE_EXIT": "no"})
        assert r.items["FOLLOWUP"].visible is True
        assert r.items["MANAGER_COMMENT"].required is True
        assert r.items["FIRE_EXIT"].comment_required and r.items["FIRE_EXIT"].comment_prompts == ["Describe the obstruction"]
        assert r.items["FIRE_EXIT"].evidence == {"kinds": ["photo"], "min_count": 1}
        assert r.messages == [{"severity": "warning", "message": "A blocked fire exit must be reported immediately.", "rule": "R-1",
                               "target": {"type": "item", "code": "FIRE_EXIT"}}]
        assert r.items["FIRE_EXIT"].messages == r.messages

    def test_scenario_a_yes(self):
        r = run(self.fire(), {"FIRE_EXIT": "yes"})
        assert r.items["FOLLOWUP"].visible is False  # a SHOW rule exists and did not match
        assert r.items["MANAGER_COMMENT"].required is False
        assert not r.items["FIRE_EXIT"].comment_required and r.items["FIRE_EXIT"].evidence is None and r.messages == []

    def test_unanswered_changes_nothing(self):
        r = run(self.fire(), {})
        assert r.items["FOLLOWUP"].visible is False and r.messages == []

    def test_scenario_b_temperature_range(self):
        s = snap([item("TEMP", "numeric", config={"min": -10, "max": 40, "unit": "°C", "decimals": 1}), item("FOLLOW", "text", required=False)], [
            rule("R", cond("TEMP", "GREATER_THAN", 5), [act("DISPLAY_MESSAGE", "TEMP", severity="error", message="Temperature above 5 °C."), act("SHOW_ITEM", "FOLLOW")])])
        high, ok = run(s, {"TEMP": "7.5"}), run(s, {"TEMP": "3"})
        assert high.messages[0]["message"] == "Temperature above 5 °C." and high.items["FOLLOW"].visible is True
        assert ok.messages == [] and ok.items["FOLLOW"].visible is False
        # No finding, no score: the result only describes state.
        assert set(high.as_dict()) == {"items", "sections", "rules", "messages", "errors", "completion", "trace"}

    def test_scenario_c_conditional_section(self):
        s = snap(
            [item("DRIVE_THRU", "yes_no"), item("DT1", section="S2"), item("DT2", "numeric", section="S2")],
            [rule("R", cond("DRIVE_THRU", "EQUALS", "yes"), [act("SHOW_SECTION", "S2", target_type="section")])],
            sections=("S1", "S2"),
        )
        yes, no = run(s, {"DRIVE_THRU": "yes"}), run(s, {"DRIVE_THRU": "no"})
        assert yes.sections["S2"]["visible"] is True and yes.items["DT1"].visible and yes.items["DT1"].required
        assert no.sections["S2"]["visible"] is False and not no.items["DT1"].visible
        assert no.items["DT1"].required is False and "DT1" not in no.completion["unanswered_required"]
        assert yes.completion["unanswered_required"] == ["DT1", "DT2"]
        assert no.completion["unanswered_required"] == []  # excluded from applicable completion
        assert no.completion["complete"] is True

    def test_scenario_e_choice_dependency(self):
        s = snap([item("CONDITION", "single_choice", options=("good", "fair", "damaged")), item("DESCRIPTION", "long_text", required=False)],
                 [rule("R", cond("CONDITION", "EQUALS", "damaged"), [act("REQUIRE_ITEM", "DESCRIPTION")])])
        assert run(s, {"CONDITION": "damaged"}).items["DESCRIPTION"].required is True
        assert run(s, {"CONDITION": "good"}).items["DESCRIPTION"].required is False


# ---------------------------------------------------------------------------
class TestActions:
    def test_hide_item_and_the_hidden_answer_is_preserved_not_counted(self):
        s = snap([item("A"), item("B")], [rule("R", cond("A", "EQUALS", "no"), [act("HIDE_ITEM", "B")])])
        r = run(s, {"A": "no", "B": "yes"})
        assert r.items["B"].visible is False and r.items["B"].required is False
        assert r.items["B"].answer.status == HIDDEN  # ignored, not deleted (the caller keeps its draft answer)
        assert r.completion["complete"] is True
        assert run(s, {"A": "yes", "B": "yes"}).items["B"].answer.status == ANSWERED  # visible again: the answer counts

    def test_make_optional_and_require(self):
        s = snap([item("A"), item("B", required=True), item("C", required=False)],
                 [rule("R1", cond("A", "EQUALS", "yes"), [act("MAKE_OPTIONAL", "B")]),
                  rule("R2", cond("A", "EQUALS", "yes"), [act("REQUIRE_ITEM", "C")])])
        r = run(s, {"A": "yes"})
        assert r.items["B"].required is False and r.items["C"].required is True
        assert run(s, {"A": "no"}).items["B"].required is True

    def test_section_hide(self):
        s = snap([item("A"), item("B", section="S2")], [rule("R", cond("A", "EQUALS", "no"), [act("HIDE_SECTION", "S2", target_type="section")])], sections=("S1", "S2"))
        r = run(s, {"A": "no"})
        assert r.sections["S2"]["visible"] is False and r.items["B"].visible is False

    def test_messages_instructions_evidence_merge_in_rule_order(self):
        s = snap([item("A", "numeric"), item("B")], [
            rule("R2", cond("A", "GREATER_THAN", 1), [act("DISPLAY_MESSAGE", message="second", severity="info")], order=2),
            rule("R1", cond("A", "GREATER_THAN", 1), [act("DISPLAY_MESSAGE", message="first", severity="warning"),
                                                       act("ADD_INSTRUCTIONS", "B", text="Use the calibrated probe."),
                                                       act("REQUIRE_EVIDENCE", "B", kinds=["file"], min_count=1)], order=1),
            rule("R3", cond("A", "GREATER_THAN", 1), [act("REQUIRE_EVIDENCE", "B", kinds=["photo"], min_count=2)], order=3),
        ])
        r = run(s, {"A": 5})
        assert [m["message"] for m in r.messages] == ["first", "second"]  # by sort order, not input order
        assert r.items["B"].instructions == ["Use the calibrated probe."]
        assert r.items["B"].evidence == {"kinds": ["photo", "file"], "min_count": 2}

    def test_option_availability(self):
        s = snap([item("AREA", "single_choice", options=("freezer", "chiller")), item("COND", "single_choice", options=("good", "fair", "poor", "n_a"))], [
            rule("R", cond("AREA", "EQUALS", "freezer"), [act("OPTION_AVAILABILITY", "COND", mode="exclude", options=["n_a"])])])
        r = run(s, {"AREA": "freezer", "COND": "n_a"})
        assert r.items["COND"].options == {"available": ["good", "fair", "poor"], "unavailable": ["n_a"]}
        assert r.items["COND"].answer.status == INVALID  # the chosen option is not available now
        assert run(s, {"AREA": "chiller", "COND": "n_a"}).items["COND"].options is None

    def test_option_restrictions_intersect_and_never_empty_the_list(self):
        s = snap([item("X"), item("Y"), item("COND", "multiple_choice", options=("a", "b", "c"))], [
            rule("R1", cond("X", "EQUALS", "yes"), [act("OPTION_AVAILABILITY", "COND", mode="only", options=["a", "b"])]),
            rule("R2", cond("Y", "EQUALS", "yes"), [act("OPTION_AVAILABILITY", "COND", mode="only", options=["b", "c"])]),
        ])
        assert run(s, {"X": "yes", "Y": "yes"}).items["COND"].options["available"] == ["b"]
        s2 = snap([item("X"), item("Y"), item("COND", "multiple_choice", options=("a", "b"))], [
            rule("R1", cond("X", "EQUALS", "yes"), [act("OPTION_AVAILABILITY", "COND", mode="only", options=["a"])]),
            rule("R2", cond("Y", "EQUALS", "yes"), [act("OPTION_AVAILABILITY", "COND", mode="only", options=["b"])])])
        r = run(s2, {"X": "yes", "Y": "yes"})
        assert r.items["COND"].options["available"] == ["a", "b"] and any(e["code"] == "options_exhausted" for e in r.errors)

    def test_validation_constraint_tightens_through_phase5(self):
        s = snap([item("AREA", "single_choice", options=("freezer", "chiller")), item("TEMP", "numeric", config={"min": -30, "max": 20, "decimals": 1})], [
            rule("R", cond("AREA", "EQUALS", "freezer"), [act("SET_VALIDATION_CONSTRAINT", "TEMP", constraints={"max": -15})])])
        r = run(s, {"AREA": "freezer", "TEMP": "-10"})
        assert r.items["TEMP"].constraints == {"max": -15} and r.items["TEMP"].answer.status == INVALID
        assert r.items["TEMP"].answer.errors[0]["code"] == "out_of_range"
        assert run(s, {"AREA": "freezer", "TEMP": "-18"}).items["TEMP"].answer.status == ANSWERED
        assert run(s, {"AREA": "chiller", "TEMP": "-10"}).items["TEMP"].answer.status == ANSWERED  # base limits again

    def test_constraints_take_the_tightest_value(self):
        s = snap([item("X"), item("Y"), item("T", "numeric", config={"min": 0, "max": 100})], [
            rule("R1", cond("X", "EQUALS", "yes"), [act("SET_VALIDATION_CONSTRAINT", "T", constraints={"max": 50})]),
            rule("R2", cond("Y", "EQUALS", "yes"), [act("SET_VALIDATION_CONSTRAINT", "T", constraints={"max": 20, "min": 5})])])
        assert run(s, {"X": "yes", "Y": "yes"}).items["T"].constraints == {"max": 20, "min": 5}

    def test_conflicting_constraints_fall_back_to_the_base_limits(self):
        s = snap([item("X"), item("Y"), item("T", "numeric", config={"min": 0, "max": 100})], [
            rule("R1", cond("X", "EQUALS", "yes"), [act("SET_VALIDATION_CONSTRAINT", "T", constraints={"max": 10})]),
            rule("R2", cond("Y", "EQUALS", "yes"), [act("SET_VALIDATION_CONSTRAINT", "T", constraints={"min": 20})])])
        assert "possible_conflict" in codes(issues(s), "warning")  # different conditions: warned, not rejected
        r = run(s, {"X": "yes", "Y": "yes", "T": "50"})
        assert any(e["code"] == "constraint_conflict" for e in r.errors) and r.items["T"].answer.status == ANSWERED

    def test_hidden_source_is_ignored_by_dependents(self):
        s = snap([item("A"), item("B"), item("C")], [
            rule("R1", cond("A", "EQUALS", "yes"), [act("SHOW_ITEM", "B")]),
            rule("R2", cond("B", "EQUALS", "yes"), [act("SHOW_ITEM", "C")])])
        assert run(s, {"A": "yes", "B": "yes"}).items["C"].visible is True
        stale = run(s, {"A": "no", "B": "yes"})  # B hidden: its stale answer must not reveal C
        assert stale.items["B"].visible is False and stale.items["C"].visible is False

    def test_invalid_n_a_claim_on_a_required_question(self):
        s = snap([item("A")], [])
        r = run(s, {}, na=["A"])
        assert r.items["A"].answer.status == INVALID and r.items["A"].answer.errors[0]["code"] == "not_applicable_not_allowed"
        assert r.completion["complete"] is False
        optional = snap([item("A", required=False)], [])
        assert run(optional, {}, na=["A"]).items["A"].answer.status == NOT_APPLICABLE

    def test_unknown_answer_keys_are_reported_and_ignored(self):
        r = run(snap([item("A")]), {"ZZZ": "x"})
        assert any(e["code"] == "unknown_item" for e in r.errors)

    def test_disabled_rules_do_nothing_but_are_listed(self):
        s = snap([item("A"), item("B")], [rule("R", None, [act("HIDE_ITEM", "B")], active=False)])
        r = run(s)
        assert r.items["B"].visible is True and r.rules == [{"code": "R", "name": "Rule R", "enabled": False, "matched": None}]


# ---------------------------------------------------------------------------
class TestConflictResolution:
    def test_hide_beats_show(self):
        s = snap([item("A"), item("B"), item("T")], [rule("R1", cond("A", "EQUALS", "yes"), [act("SHOW_ITEM", "T")]), rule("R2", cond("B", "EQUALS", "yes"), [act("HIDE_ITEM", "T")])])
        assert "possible_conflict" in codes(issues(s), "warning") and issues(s).ok
        assert run(s, {"A": "yes", "B": "yes"}).items["T"].visible is False
        assert run(s, {"A": "yes", "B": "no"}).items["T"].visible is True

    def test_required_beats_optional(self):
        s = snap([item("A"), item("B"), item("T", required=False)], [rule("R1", cond("A", "EQUALS", "yes"), [act("MAKE_OPTIONAL", "T")]), rule("R2", cond("B", "EQUALS", "yes"), [act("REQUIRE_ITEM", "T")])])
        assert run(s, {"A": "yes", "B": "yes"}).items["T"].required is True

    def test_identical_conditions_with_opposite_actions_are_rejected(self):
        s = snap([item("A"), item("T")], [rule("R1", cond("A", "EQUALS", "yes"), [act("SHOW_ITEM", "T")]), rule("R2", cond("A", "EQUALS", "yes"), [act("HIDE_ITEM", "T")])])
        assert "conflict" in codes(issues(s))
        s = snap([item("A"), item("T")], [rule("R1", None, [act("REQUIRE_ITEM", "T")]), rule("R2", None, [act("MAKE_OPTIONAL", "T")])])
        assert "conflict" in codes(issues(s))

    def test_one_rule_cannot_contradict_itself(self):
        s = snap([item("A"), item("T")], [rule("R", cond("A", "EQUALS", "yes"), [act("SHOW_ITEM", "T"), act("HIDE_ITEM", "T")])])
        assert "conflict" in codes(issues(s))

    def test_result_is_independent_of_rule_input_order(self):
        rules = [
            rule(f"R{i}", cond("A", "EQUALS", "yes"), [act("DISPLAY_MESSAGE", message=f"m{i}")], order=i) for i in range(1, 8)
        ]
        base = snap([item("A")], rules)
        expected = run(base, {"A": "yes"}).as_dict()
        for seed in range(5):
            shuffled = list(rules)
            random.Random(seed).shuffle(shuffled)
            assert run(snap([item("A")], shuffled), {"A": "yes"}).as_dict() == expected

    def test_same_inputs_same_outputs(self):
        s = TestScenarios().fire()
        assert run(s, {"FIRE_EXIT": "no"}).as_dict() == run(s, {"FIRE_EXIT": "no"}).as_dict()


# ---------------------------------------------------------------------------
class TestDependencyValidation:
    def base(self):
        return [item("A"), item("N", "numeric", config={"min": 0, "max": 20}), item("C", "single_choice", options=("x", "y")), item("T", "long_text", required=False)]

    def check(self, condition=None, actions=None):
        return issues(snap(self.base(), [rule("R", condition, actions or [act("SHOW_ITEM", "T")])]))

    def test_valid_rule(self):
        assert self.check(cond("A", "EQUALS", "yes")).ok

    def test_unknown_source_and_target(self):
        assert "unknown_item" in codes(self.check(cond("NOPE", "EQUALS", "yes")))
        assert "invalid_action" in codes(self.check(cond("A", "EQUALS", "yes"), [act("SHOW_ITEM", "NOPE")]))
        assert "invalid_action" in codes(self.check(cond("A", "EQUALS", "yes"), [act("SHOW_SECTION", "S9", target_type="section")]))

    def test_item_from_another_version_is_unknown(self):
        other = snap([item("FOREIGN")])
        report = issues(snap(self.base(), [rule("R", cond("FOREIGN", "EQUALS", "yes"), [act("SHOW_ITEM", "T")])]))
        assert "unknown_item" in codes(report) and "FOREIGN" in other.items  # exists elsewhere, not here

    def test_operator_must_suit_the_source_type(self):
        report = self.check(cond("A", "GREATER_THAN", 5))
        assert "invalid_operator" in codes(report) and "EQUALS" in report.errors[0].message
        assert "invalid_operator" in codes(self.check(cond("N", "CONTAINS", "x")))
        assert "invalid_operator" in codes(self.check(cond("A", "FROBNICATE", 1)))

    @pytest.mark.parametrize("comparison,value", [
        ("EQUALS", "maybe"), ("EQUALS", 5), ("EQUALS", None), ("IS_EMPTY", "x"),
    ])
    def test_bad_values_on_a_yes_no_source(self, comparison, value):
        assert "invalid_value" in codes(self.check(cond("A", comparison, value)))

    def test_bad_numeric_and_range_values(self):
        for comparison, value in (("GREATER_THAN", "abc"), ("GREATER_THAN", None), ("BETWEEN", 5), ("BETWEEN", [1]), ("BETWEEN", [9, 1]), ("BETWEEN", ["a", "b"])):
            assert "invalid_value" in codes(self.check(cond("N", comparison, value))), (comparison, value)

    def test_option_references_must_exist(self):
        assert "invalid_value" in codes(self.check(cond("C", "EQUALS", "zzz")))
        assert "invalid_value" in codes(self.check(cond("C", "SELECTED", "zzz")))
        assert "invalid_value" in codes(self.check(cond("C", "CONTAINS_ANY", ["x", "zzz"])))
        assert "invalid_value" in codes(self.check(cond("C", "CONTAINS_ANY", [])))
        assert "invalid_value" in codes(self.check(cond("C", "CONTAINS_ANY", ["x", "x"])))
        assert self.check(cond("C", "CONTAINS_ANY", ["x", "y"])).ok

    def test_text_values(self):
        s = snap([item("T1", "text"), item("T")], [rule("R", cond("T1", "CONTAINS", "x" * 500), [act("HIDE_ITEM", "T")])])
        assert "invalid_value" in codes(issues(s))
        s = snap([item("T1", "text"), item("T")], [rule("R", cond("T1", "CONTAINS", "  "), [act("HIDE_ITEM", "T")])])
        assert "invalid_value" in codes(issues(s))

    def test_action_target_types_must_match(self):
        assert "invalid_action" in codes(self.check(None, [act("SHOW_ITEM", "S1", target_type="section")]))
        assert "invalid_action" in codes(self.check(None, [act("SHOW_SECTION", "T", target_type="item")]))
        assert "invalid_action" in codes(self.check(None, [act("HIDE_ITEM")]))  # needs a target
        assert "invalid_action" in codes(self.check(None, [act("SHOW_ITEM", "T", foo=1)]))  # unexpected parameter
        assert "invalid_action" in codes(self.check(None, [act("DELETE_ALL", "T")]))

    def test_action_parameters(self):
        for bad in (
            act("DISPLAY_MESSAGE", message=""), act("DISPLAY_MESSAGE", severity="fatal", message="x"),
            act("DISPLAY_MESSAGE", message="x" * 400), act("DISPLAY_MESSAGE", message="a\x00b"),
            act("REQUIRE_EVIDENCE", "T", kinds=["exe"]), act("REQUIRE_EVIDENCE", "T", kinds=[]), act("REQUIRE_EVIDENCE", "T", min_count=0),
            act("REQUIRE_COMMENT", "T", prompt="x" * 300), act("ADD_INSTRUCTIONS", "T"),
            act("OPTION_AVAILABILITY", "C", mode="maybe", options=["x"]), act("OPTION_AVAILABILITY", "C", mode="only", options=["zzz"]),
            act("OPTION_AVAILABILITY", "C", mode="exclude", options=["x", "y"]),  # leaves nothing
            act("OPTION_AVAILABILITY", "A", mode="only", options=["yes"]),  # not a choice question
            act("SET_VALIDATION_CONSTRAINT", "N", constraints={"max": 30}),  # loosens the base limit of 20
            act("SET_VALIDATION_CONSTRAINT", "N", constraints={"min": 15, "max": 10}),
            act("SET_VALIDATION_CONSTRAINT", "N", constraints={"decimals": 0}),  # not a rule constraint
            act("SET_VALIDATION_CONSTRAINT", "A", constraints={"max": 1}),  # yes/no has none
            act("SET_VALIDATION_CONSTRAINT", "N", constraints={}),
        ):
            assert "invalid_action" in codes(self.check(None, [bad])), bad

    def test_a_good_constraint_and_message(self):
        assert self.check(None, [act("SET_VALIDATION_CONSTRAINT", "N", constraints={"max": 15}), act("DISPLAY_MESSAGE", message="Careful", severity="warning")]).ok

    def test_unknown_keys_cannot_smuggle_behaviour(self):
        assert "invalid_condition" in codes(self.check({"operator": "AND", "conditions": [cond("A", "EQUALS", "yes")], "script": "x"}))
        assert "invalid_condition" in codes(self.check({"source_item_code": "A", "comparison": "EQUALS", "value": "yes", "eval": "1"}))

    def test_actions_are_required_and_limited(self, settings):
        assert "invalid_rule" in codes(issues(snap(self.base(), [rule("R", None, [])])))
        assert "invalid_rule" in codes(issues(snap(self.base(), [RuleSpec("R", "n", None, "SHOW_ITEM")])))
        settings.CHECKLIST_RULE_LIMITS = {"max_actions_per_rule": 1}
        assert "too_many" in codes(issues(snap(self.base(), [rule("R", None, [act("SHOW_ITEM", "T"), act("HIDE_ITEM", "T")])])))

    def test_rule_count_and_duplicate_codes(self, settings):
        s = snap(self.base(), [rule("R", None, [act("SHOW_ITEM", "T")]), rule("R", None, [act("SHOW_ITEM", "T")])])
        assert "duplicate_code" in codes(issues(s))
        settings.CHECKLIST_RULE_LIMITS = {"max_rules_per_version": 1}
        s = snap(self.base(), [rule("R1", None, [act("DISPLAY_MESSAGE", message="a")]), rule("R2", None, [act("DISPLAY_MESSAGE", message="b")])])
        assert "too_many" in codes(issues(s))

    def test_all_problems_are_reported_together(self):
        report = self.check(group("AND", cond("NOPE", "EQUALS", "x"), cond("A", "GREATER_THAN", 1)), [act("SHOW_ITEM", "ALSO_NOPE")])
        assert len(report.errors) >= 3

    def test_inactive_references_warn(self):
        s = snap([item("A", active=False), item("T")], [rule("R", cond("A", "EQUALS", "yes"), [act("SHOW_ITEM", "T")])])
        assert issues(s).ok and "inactive_source" in codes(issues(s), "warning")

    def test_injection_strings_are_just_data(self):
        payloads = ["__import__('os').system('x')", "{{7*7}}", "<script>alert(1)</script>", "'; DROP TABLE users;--", "${jndi:ldap://x}"]
        for payload in payloads:
            s = snap([item("TXT", "text"), item("T")], [rule("R", cond("TXT", "CONTAINS", payload), [act("DISPLAY_MESSAGE", message=payload)])])
            assert issues(s).ok
            r = run(s, {"TXT": f"prefix {payload} suffix"})
            assert r.rules[0]["matched"] == "true" and r.messages[0]["message"] == payload  # stored and echoed verbatim, never evaluated


# ---------------------------------------------------------------------------
class TestCycles:
    def test_direct_cycle(self):
        s = snap([item("A"), item("B")], [rule("R1", cond("A", "EQUALS", "yes"), [act("SHOW_ITEM", "B")]), rule("R2", cond("B", "EQUALS", "yes"), [act("SHOW_ITEM", "A")])])
        report = issues(s)
        assert "cycle" in codes(report)
        message = next(i.message for i in report.errors if i.code == "cycle")
        assert "R1" in message and "R2" in message and "circle" in message

    def test_indirect_cycle(self):
        s = snap([item("A"), item("B"), item("C")], [
            rule("R1", cond("A", "EQUALS", "yes"), [act("SHOW_ITEM", "B")]),
            rule("R2", cond("B", "EQUALS", "yes"), [act("SHOW_ITEM", "C")]),
            rule("R3", cond("C", "EQUALS", "yes"), [act("HIDE_ITEM", "A")])])
        assert "cycle" in codes(issues(s))

    def test_self_dependency_through_visibility(self):
        s = snap([item("A")], [rule("R", cond("A", "EQUALS", "yes"), [act("HIDE_ITEM", "A")])])
        assert "cycle" in codes(issues(s))

    def test_section_containment_cycle(self):
        # The question that reveals section S2 lives inside S2: hiding it would hide its own switch.
        s = snap([item("A", section="S2"), item("B")], [rule("R", cond("A", "EQUALS", "yes"), [act("SHOW_SECTION", "S2", target_type="section")])], sections=("S1", "S2"))
        assert "cycle" in codes(issues(s))

    def test_valid_acyclic_chain(self):
        s = snap([item("A"), item("B"), item("C"), item("D")], [
            rule("R1", cond("A", "EQUALS", "yes"), [act("SHOW_ITEM", "B")]),
            rule("R2", cond("B", "EQUALS", "yes"), [act("SHOW_ITEM", "C")]),
            rule("R3", cond("A", "EQUALS", "yes"), [act("SHOW_ITEM", "D")]),
            rule("R4", cond("C", "EQUALS", "yes"), [act("SHOW_ITEM", "D")])])
        assert issues(s).ok

    def test_cross_section_dependency_is_fine(self):
        s = snap([item("A"), item("B", section="S2")], [rule("R", cond("A", "EQUALS", "yes"), [act("SHOW_SECTION", "S2", target_type="section")])], sections=("S1", "S2"))
        assert issues(s).ok

    def test_annotation_actions_do_not_create_cycles(self):
        s = snap([item("A"), item("B")], [
            rule("R1", cond("A", "EQUALS", "yes"), [act("REQUIRE_ITEM", "B")]),
            rule("R2", cond("B", "EQUALS", "yes"), [act("REQUIRE_ITEM", "A")]),
            rule("R3", cond("A", "EQUALS", "no"), [act("REQUIRE_COMMENT", "A"), act("DISPLAY_MESSAGE", "A", message="x")])])
        assert issues(s).ok

    def test_disabled_rules_do_not_count_for_cycles(self):
        s = snap([item("A"), item("B")], [
            rule("R1", cond("A", "EQUALS", "yes"), [act("SHOW_ITEM", "B")]),
            rule("R2", cond("B", "EQUALS", "yes"), [act("SHOW_ITEM", "A")], active=False)])
        assert issues(s).ok

    def test_the_evaluator_refuses_a_cyclic_set(self):
        s = snap([item("A"), item("B")], [rule("R1", cond("A", "EQUALS", "yes"), [act("SHOW_ITEM", "B")]), rule("R2", cond("B", "EQUALS", "yes"), [act("SHOW_ITEM", "A")])])
        r = run(s, {"A": "yes"})
        assert any(e["code"] == "cycle" for e in r.errors) and r.completion["complete"] is False
        assert all(item_["visible"] for item_ in (v for v in r.as_dict()["items"].values()))  # nothing was applied

    def test_evaluation_order_is_dependency_order(self):
        s = snap([item("A"), item("B"), item("C")], [
            rule("R2", cond("B", "EQUALS", "yes"), [act("SHOW_ITEM", "C")], order=1),  # listed first, depends on R1
            rule("R1", cond("A", "EQUALS", "yes"), [act("SHOW_ITEM", "B")], order=2)])
        r = run(s, {"A": "yes", "B": "yes"})
        assert r.items["B"].visible and r.items["C"].visible


# ---------------------------------------------------------------------------
class TestCompletionContract:
    def test_hidden_items_are_not_treated_as_answered(self):
        s = snap([item("A"), item("B", required=False)], [rule("R", cond("A", "EQUALS", "no"), [act("HIDE_ITEM", "B")])])
        r = run(s, {"A": "no", "B": "yes"})
        assert r.items["B"].answer.status == HIDDEN and r.items["B"].answer.status != ANSWERED

    def test_completion_summary(self):
        s = snap([item("A"), item("B", "numeric"), item("C", required=False)], [])
        r = run(s, {"A": "yes", "B": "abc"})
        assert r.completion == {"applicable_required": 2, "unanswered_required": [], "invalid": ["B"], "complete": False}
        assert run(s, {"A": "yes", "B": 1}).completion["complete"] is True

    def test_frontend_contract_shape(self):
        data = run(TestScenarios().fire(), {"FIRE_EXIT": "no"}).as_dict()
        entry = data["items"]["MANAGER_COMMENT"]
        assert {"visible", "required", "comment_required", "evidence", "options", "constraints", "instructions", "messages", "answer"} <= set(entry)
        assert data["messages"][0]["severity"] == "warning" and data["messages"][0]["message"]
        import json

        json.dumps(data)  # JSON-safe

    def test_snapshot_has_no_state_between_runs(self):
        s = TestScenarios().fire()
        compiled = compile_rules(s)
        first = evaluate(compiled, {"FIRE_EXIT": "no"}).as_dict()
        evaluate(compiled, {"FIRE_EXIT": "yes"})
        assert evaluate(compiled, {"FIRE_EXIT": "no"}).as_dict() == first


# ---------------------------------------------------------------------------
class TestDescribe:
    def test_readable_summary(self):
        s = snap([item("FIRE", question="Is the fire exit accessible?"), item("COMMENT", "long_text", question="Manager comment")], [])
        text = describe_rule(s, {"operator": "AND", "conditions": [{"source_item_code": "FIRE", "comparison": "EQUALS", "value": "no"}]},
                             [{"type": "REQUIRE_ITEM", "target": {"type": "item", "code": "COMMENT"}}])
        assert text == "If “Is the fire exit accessible?” equals No, make required “Manager comment”."

    def test_group_and_always(self):
        s = snap([item("A"), item("B", "numeric", config={"unit": "kg"})], [])
        cond_ = group("OR", cond("A", "EQUALS", "yes"), cond("B", "BETWEEN", ["1", "5"]))
        text = describe_rule(s, cond_, [{"type": "DISPLAY_MESSAGE", "severity": "warning", "message": "Heads up"}])
        assert "or" in text and "is between 1 kg – 5 kg" in text and "display a warning message" in text
        assert describe_rule(s, None, [{"type": "DISPLAY_MESSAGE", "severity": "info", "message": "x"}]).startswith("Always,")


def test_limits_have_documented_defaults():
    assert rule_limits.limit("max_group_depth") == 3 and rule_limits.limit("max_rules_per_version") == 200
    assert set(rule_limits.all_limits()) == set(rule_limits.DEFAULTS)
