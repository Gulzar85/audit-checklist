"""Evaluation rules: schema, validation, three-valued semantics, ordering and conflicts, explanations - pure unit tests, no database."""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace as NS

import pytest

from apps.checklists.answer_types import registry
from apps.checklists.rules.snapshot import ItemSpec, OptionSpec
from apps.checklists.rules.state import ANSWERED, EMPTY, HIDDEN, INVALID, NOT_APPLICABLE, AnswerState
from apps.checklists.rules.tri import Tri, tri_not
from apps.evaluation import config as cfg
from apps.evaluation import constants as k
from apps.evaluation import sources
from apps.evaluation.engine import Inputs, evaluate, input_fingerprint


def item(code, atype, options=(), question=None, **config):
    return ItemSpec(id=f"id-{code}", code=code, section_code="S-001", question=question or f"Question {code}", answer_type=atype,
                    config=registry.clean_config(atype, config), required=True, active=True, sort_order=1,
                    options=tuple(OptionSpec(f"o-{v}", v, v.title(), n, True) for n, v in enumerate(options)))


SNAP = NS(items={i.code: i for i in (item("Q-101", "yes_no"), item("TEMP", "numeric", min=0, max=100, decimals=1), item("KIND", "single_choice", ["dry", "cold"]),
                                     item("TAGS", "multiple_choice", ["pest", "leak"]), item("NOTE", "text"))}, sections={})


def leaf(source, field, op, value=None):
    node = {"source": source, "field": field, "operator": op}
    if value is not None:
        node["value"] = value
    return node


def rule(code="R1", priority=10, condition=None, outcome="fail", **extra):
    return {"code": code, "name": f"Rule {code}", "priority": priority, "condition": condition or leaf("answer", "Q-101", "EQUALS", "no"), "outcome": outcome, **extra}


def compile_rules(*rules, enabled=True, lookups=None):
    issues, compiled, doc = cfg.validate_document(enabled, {"rules": list(rules)}, SNAP, lookups)
    return issues, compiled, doc


def errors(issues):
    return {i.code for i in issues if i.level == "error"}


def answered(value):
    return AnswerState(ANSWERED, value, value)


def inputs(answers=None, score=None, execution=None):
    return Inputs({c: answered(v) if not isinstance(v, AnswerState) else v for c, v in (answers or {}).items()},
                  sources.score_states(None) if score is None else score, execution or {"status": answered("submitted")})


def run(*rules, **kw):
    issues, compiled, _doc = compile_rules(*rules)
    assert not errors(issues), issues
    return evaluate(compiled, inputs(**kw))


def final_score(percentage="62", grade="D", critical=False):
    return sources.synthetic_score_states({"status": "final", "percentage": percentage, "earned": "10", "available": "16", "grade": grade, "critical": critical})


# ---------------------------------------------------------------------------------------------------------------- semantics
class TestTriNot:
    def test_not_keeps_unknown_unknown(self):
        assert (tri_not(Tri.TRUE), tri_not(Tri.FALSE), tri_not(Tri.UNKNOWN)) == (Tri.FALSE, Tri.TRUE, Tri.UNKNOWN)


class TestLeafSemantics:
    @pytest.mark.parametrize("value,expected", [("no", "fail"), ("yes", "pass")])
    def test_answer_equality(self, value, expected):
        assert run(rule(), answers={"Q-101": value}).outcome == expected

    @pytest.mark.parametrize("answer,op,value,matches", [
        ("4.5", "GREATER_THAN", "4", True), ("4", "GREATER_THAN", "4", False), ("4", "GREATER_THAN_OR_EQUAL", "4", True),
        ("3.9", "LESS_THAN", "4", True), ("4", "LESS_THAN_OR_EQUAL", "4", True), ("5", "BETWEEN", ["5", "10"], True), ("10.1", "BETWEEN", ["5", "10"], False),
        ("11", "NOT_BETWEEN", ["5", "10"], True)])
    def test_numeric_comparisons_and_range_boundaries(self, answer, op, value, matches):
        parsed = registry.get("numeric").parse_operand(answer, SNAP.items["TEMP"].context())
        out = run(rule(condition=leaf("answer", "TEMP", op, value)), answers={"TEMP": parsed})
        assert (out.outcome == "fail") is matches

    def test_choice_and_multi_choice_operators(self):
        assert run(rule(condition=leaf("answer", "KIND", "EQUALS", "cold")), answers={"KIND": "cold"}).outcome == "fail"
        assert run(rule(condition=leaf("answer", "TAGS", "CONTAINS_ALL", ["pest", "leak"])), answers={"TAGS": ["pest", "leak"]}).outcome == "fail"
        assert run(rule(condition=leaf("answer", "TAGS", "CONTAINS_ANY", ["leak"])), answers={"TAGS": ["pest"]}).outcome == "pass"

    def test_text_operators_are_case_insensitive(self):
        assert run(rule(condition=leaf("answer", "NOTE", "CONTAINS", "BLOCKED")), answers={"NOTE": "Exit is blocked by crates"}).outcome == "fail"

    @pytest.mark.parametrize("state,op,result", [
        (EMPTY, "IS_EMPTY", "fail"), (EMPTY, "IS_NOT_EMPTY", "pass"), (NOT_APPLICABLE, "IS_EMPTY", "pass"), (NOT_APPLICABLE, "IS_NOT_EMPTY", "pass"),
        (INVALID, "IS_EMPTY", "pass"), (HIDDEN, "IS_NOT_EMPTY", "pass")])
    def test_presence_operators_and_missing_values(self, state, op, result):
        assert run(rule(condition=leaf("answer", "Q-101", op)), answers={"Q-101": AnswerState(state)}).outcome == result

    @pytest.mark.parametrize("state", [EMPTY, NOT_APPLICABLE, INVALID, HIDDEN])
    def test_a_missing_answer_is_unknown_never_false_or_zero(self, state):
        out = run(rule(condition=leaf("answer", "TEMP", "LESS_THAN", "5")), answers={"TEMP": AnswerState(state)})
        assert out.rules[0]["status"] == "unknown" and out.undecided == ["R1"] and out.outcome == "pass" and out.matched == []

    def test_a_question_absent_from_the_inputs_is_unknown(self):
        assert run(rule(), answers={}).rules[0]["status"] == "unknown"


class TestGroups:
    A, B = leaf("answer", "Q-101", "EQUALS", "no"), leaf("answer", "KIND", "EQUALS", "cold")

    @pytest.mark.parametrize("logic,q,kind,expected", [
        ("AND", "no", "cold", "matched"), ("AND", "no", "dry", "unmatched"), ("AND", "yes", "dry", "unmatched"),
        ("OR", "no", "dry", "matched"), ("OR", "yes", "cold", "matched"), ("OR", "yes", "dry", "unmatched")])
    def test_and_or(self, logic, q, kind, expected):
        out = run(rule(condition={"logic": logic, "conditions": [self.A, self.B]}), answers={"Q-101": q, "KIND": kind})
        assert out.rules[0]["status"] == expected

    def test_kleene_unknown_handling(self):
        unknown = AnswerState(EMPTY)
        and_rule = rule(condition={"logic": "AND", "conditions": [self.A, self.B]})
        or_rule = rule("R2", 20, condition={"logic": "OR", "conditions": [self.A, self.B]})
        out = run(and_rule, or_rule, answers={"Q-101": "no", "KIND": unknown})
        assert [r["status"] for r in out.rules] == ["unknown", "matched"]  # AND(TRUE, UNKNOWN) = UNKNOWN; OR(TRUE, UNKNOWN) = TRUE
        out = run(and_rule, or_rule, answers={"Q-101": "yes", "KIND": unknown})
        assert [r["status"] for r in out.rules] == ["unmatched", "unknown"]  # AND(FALSE, UNKNOWN) = FALSE; OR(FALSE, UNKNOWN) = UNKNOWN

    def test_not(self):
        negated = rule(condition={"logic": "NOT", "conditions": [self.A]})
        assert run(negated, answers={"Q-101": "yes"}).rules[0]["status"] == "matched"
        assert run(negated, answers={"Q-101": "no"}).rules[0]["status"] == "unmatched"
        assert run(negated, answers={"Q-101": AnswerState(EMPTY)}).rules[0]["status"] == "unknown"  # not knowing is not the opposite of knowing

    def test_nested_groups(self):
        c = {"logic": "AND", "conditions": [leaf("score", "percentage", "LESS_THAN", "70"), {"logic": "OR", "conditions": [leaf("score", "critical", "EQUALS", True), self.A]}]}
        assert run(rule(condition=c), answers={"Q-101": "no"}, score=final_score("62")).matched == ["R1"]
        assert run(rule(condition=c), answers={"Q-101": "yes"}, score=final_score("62")).matched == []
        assert run(rule(condition=c), answers={"Q-101": "yes"}, score=final_score("62", critical=True)).matched == ["R1"]


# ---------------------------------------------------------------------------------------------------------------- score and execution sources
class TestScoreSource:
    LOW = rule(condition=leaf("score", "percentage", "LESS_THAN", "70"), outcome="review_required")

    def test_a_final_score_below_the_threshold_matches_with_the_real_number_in_the_explanation(self):
        out = run(self.LOW, score=final_score("62"))
        assert out.outcome == "review_required"
        assert "score percentage is less than 70 (observed: 62)" in out.explanation[0] and "REVIEW REQUIRED" in out.explanation[0]

    def test_grade_and_critical_flag(self):
        assert run(rule(condition=leaf("score", "grade", "EQUALS", "d")), score=final_score(grade="D")).matched == ["R1"]  # grades compare case-insensitively
        assert run(rule(condition=leaf("score", "critical", "EQUALS", True)), score=final_score(critical=True)).matched == ["R1"]

    @pytest.mark.parametrize("status", ["provisional", "not_applicable"])
    def test_provisional_and_not_applicable_scores_are_unavailable_not_zero(self, status):
        score = sources.synthetic_score_states({"status": status, "percentage": "10"})
        out = run(self.LOW, score=score)
        assert out.rules[0]["status"] == "unknown" and out.matched == []

    def test_no_score_is_unavailable_and_the_status_says_why(self):
        assert run(self.LOW).rules[0]["status"] == "unknown"
        out = run(rule(condition=leaf("score", "status", "EQUALS", "unavailable"), outcome="review_required"))
        assert out.outcome == "review_required"

    def test_the_score_status_itself_is_always_available(self):
        out = run(rule(condition=leaf("score", "status", "EQUALS", "provisional")), score=sources.synthetic_score_states({"status": "provisional"}))
        assert out.matched == ["R1"]

    def test_the_score_is_never_recalculated_here(self):
        import apps.evaluation.engine as engine
        import apps.evaluation.sources as src

        assert "calculate" not in vars(engine) and "calculate" not in vars(src)

    def test_execution_status(self):
        assert run(rule(condition=leaf("execution", "status", "EQUALS", "submitted")), score=final_score()).matched == ["R1"]


# ---------------------------------------------------------------------------------------------------------------- ordering, conflicts, stop
class TestOrderingAndConflicts:
    W = rule("W", 30, outcome="warning")
    R = rule("R", 20, outcome="review_required")
    F = rule("F", 10, outcome="fail")

    def test_highest_precedence_wins_and_every_match_is_listed(self):
        out = run(self.W, self.R, self.F, answers={"Q-101": "no"})
        assert out.outcome == "fail" and out.matched == ["F", "R", "W"] and out.critical
        assert "highest-precedence" in " ".join(out.explanation)

    def test_a_milder_rule_never_hides_a_critical_one_whatever_the_input_order(self):
        assert run(self.F, self.W, answers={"Q-101": "no"}).outcome == run(self.W, self.F, answers={"Q-101": "no"}).outcome == "fail"

    def test_rules_run_by_priority_then_code_not_by_input_order(self):
        issues, _c, _d = compile_rules(rule("B", 10), rule("A", 10))
        assert "duplicate_priority" in errors(issues)  # equal priorities are a validation error, so the order is always explicit
        out = run(rule("B", 20), rule("A", 10, outcome="warning"), answers={"Q-101": "no"})
        assert [r["code"] for r in out.rules] == ["A", "B"]

    def test_stop_processing_skips_the_rest_and_records_it(self):
        out = run(rule("A", 10, outcome="pass", stop=True), rule("B", 20), answers={"Q-101": "no"})
        assert [r["status"] for r in out.rules] == ["matched", "skipped"] and out.outcome == "pass"
        assert "stopped processing" in out.rules[1]["explanation"]

    def test_disabled_rules_never_run(self):
        out = run(rule("A", 10, enabled=False), answers={"Q-101": "no"})
        assert out.rules[0]["status"] == "disabled" and out.outcome == "not_applicable"

    def test_unknown_rules_follow_their_policy(self):
        skip = run(rule(condition=leaf("answer", "TEMP", "LESS_THAN", "5")), answers={"TEMP": AnswerState(EMPTY)})
        assert skip.outcome == "pass"
        flagged = run(rule(condition=leaf("answer", "TEMP", "LESS_THAN", "5"), on_unknown="indeterminate"), answers={"TEMP": AnswerState(EMPTY)})
        assert flagged.outcome == "indeterminate"
        both = run(rule(condition=leaf("answer", "TEMP", "LESS_THAN", "5"), on_unknown="indeterminate"), rule("B", 20, outcome="warning"), answers={"TEMP": AnswerState(EMPTY), "Q-101": "no"})
        assert both.outcome == "indeterminate"  # outranks a warning
        failing = run(rule(condition=leaf("answer", "TEMP", "LESS_THAN", "5"), on_unknown="indeterminate"), rule("B", 20), answers={"TEMP": AnswerState(EMPTY), "Q-101": "no"})
        assert failing.outcome == "fail"  # a definite failure is never softened to indeterminate

    def test_no_rules_or_nothing_matched(self):
        assert evaluate([], inputs()).outcome == "not_applicable"
        assert run(self.F, answers={"Q-101": "yes"}).outcome == "pass"

    def test_actions_are_requested_only_by_matched_rules_in_order(self):
        tagged = rule("A", 10, actions=[{"type": "tag", "tag": "critical"}, {"type": "tag", "tag": "safety"}])
        out = run(tagged, rule("B", 20, outcome="warning", actions=[{"type": "tag", "tag": "b"}]), answers={"Q-101": "no"})
        assert [(a["rule"], a["index"], a["action"]["tag"]) for a in out.actions] == [("A", 0, "critical"), ("A", 1, "safety"), ("B", 0, "b")]
        assert run(tagged, answers={"Q-101": "yes"}).actions == []

    def test_determinism(self):
        args = (self.W, self.R, self.F)
        first = run(*args, answers={"Q-101": "no"}).as_dict()
        assert first == run(*reversed(args), answers={"Q-101": "no"}).as_dict() == run(*args, answers={"Q-101": "no"}).as_dict()


# ---------------------------------------------------------------------------------------------------------------- validation
class TestValidation:
    def test_a_valid_rule_normalizes(self):
        issues, compiled, doc = compile_rules(rule(condition=leaf("score", "percentage", "LESS_THAN", "70.0")))
        assert errors(issues) == set() and doc["rules"][0]["condition"]["value"] == "70"

    @pytest.mark.parametrize("condition,code", [
        (leaf("db", "x", "EQUALS", "1"), "unknown_source"), (leaf("answer", "NOPE", "EQUALS", "no"), "unknown_item"),
        (leaf("score", "ssn", "EQUALS", "1"), "unknown_field"), (leaf("answer", "Q-101", "LESS_THAN", "3"), "invalid_operator"),
        (leaf("score", "grade", "LESS_THAN", "3"), "invalid_operator"), (leaf("answer", "Q-101", "EVAL", "1"), "unknown_operator"),
        (leaf("score", "percentage", "LESS_THAN", "abc"), "invalid_value"), (leaf("score", "percentage", "LESS_THAN", 1.5), "invalid_value"),
        (leaf("score", "percentage", "LESS_THAN", True), "invalid_value"), (leaf("score", "percentage", "LESS_THAN", "NaN"), "invalid_value"),
        (leaf("score", "critical", "EQUALS", "yes"), "invalid_value"), (leaf("score", "status", "EQUALS", "done"), "invalid_value"),
        (leaf("answer", "KIND", "EQUALS", "frozen"), "invalid_value"), ({"source": "score", "field": "grade", "operator": "EQUALS", "value": "A", "code": "x"}, "malformed"),
        ({"logic": "XOR", "conditions": [leaf("score", "critical", "EQUALS", True)]}, "malformed"), ({"logic": "AND", "conditions": []}, "malformed"),
        ({"logic": "NOT", "conditions": [leaf("score", "critical", "EQUALS", True)] * 2}, "malformed"), ("__import__('os')", "malformed"), (["a"], "malformed")])
    def test_malformed_and_malicious_conditions_are_refused(self, condition, code):
        assert code in errors(compile_rules(rule(condition=condition))[0])

    def test_depth_and_size_limits(self):
        c = leaf("score", "critical", "EQUALS", True)
        for _ in range(k.MAX_DEPTH):
            c = {"logic": "AND", "conditions": [c]}
        assert "too_deep" in errors(compile_rules(rule(condition=c))[0])
        wide = {"logic": "AND", "conditions": [leaf("score", "percentage", "LESS_THAN", str(i)) for i in range(k.MAX_CONDITIONS + 1)]}
        assert "too_many_conditions" in errors(compile_rules(rule(condition=wide))[0])
        assert "too_many_rules" in errors(cfg.validate_document(True, {"rules": [rule(f"R{i}", i) for i in range(k.MAX_RULES + 1)]}, SNAP)[0])

    @pytest.mark.parametrize("patch,code", [({"priority": -1}, "invalid_priority"), ({"priority": "5"}, "invalid_priority"), ({"outcome": "indeterminate"}, "invalid_outcome"),
                                            ({"on_unknown": "guess"}, "invalid_on_unknown"), ({"stop": "yes"}, "invalid_stop"), ({"code": "bad code!"}, "invalid_code"),
                                            ({"name": ""}, "invalid_name"), ({"enabled": 1}, "invalid_enabled"), ({"message": "x" * 301}, "invalid_message"),
                                            ({"script": "1"}, "malformed"), ({"condition": None}, "condition_required")])
    def test_rule_fields(self, patch, code):
        raw = {**rule(), **patch}
        if patch.get("condition", 1) is None:
            raw["condition"] = None
        assert code in errors(compile_rules(raw)[0])

    def test_duplicate_codes_and_priorities(self):
        assert {"duplicate_code", "duplicate_priority"} <= errors(compile_rules(rule("A", 10), rule("A", 10))[0])

    def test_document_shape(self):
        for bad in ({"rules": "x"}, {"rules": [], "eval": 1}, [], None):
            assert "malformed" in errors(cfg.validate_document(True, bad, SNAP)[0])
        assert "invalid_enabled" in errors(cfg.validate_document("yes", {"rules": []}, SNAP)[0])

    def test_actions(self):
        class L(cfg.Lookups):
            def category(self, code):
                return code == "safety"

            def severity(self, code):
                return code == "high"

            def workflow(self, code):
                return code == "exec-review"

        ok = [{"type": "request_finding", "category": "safety", "severity": "high", "title": "Low score"}, {"type": "start_workflow", "workflow": "exec-review"}, {"type": "tag", "tag": "x"}]
        assert errors(compile_rules(rule(actions=ok), lookups=L())[0]) == set()
        bad = [{"type": "request_finding", "category": "nope", "severity": "high", "title": "t"}, {"type": "start_workflow", "workflow": "nope"}, {"type": "shell", "cmd": "rm"},
               {"type": "tag", "tag": "bad;tag"}, {"type": "request_finding", "category": "safety", "severity": "high"}]
        assert "malformed" in errors(compile_rules(rule(actions=[{"type": "tag", "tag": "x", "extra": 1}]))[0])
        issues = compile_rules(rule(actions=bad), lookups=L())[0]
        assert {"unknown_reference", "unknown_action", "invalid_action"} <= errors(issues)
        assert "invalid_actions" in errors(compile_rules(rule(actions=[{"type": "tag", "tag": "x"}] * (k.MAX_ACTIONS + 1)))[0])

    def test_hash_is_stable_and_sensitive(self):
        _i, _c, doc = compile_rules(rule())
        _i, _c, same = compile_rules(rule())
        _i, _c, other = compile_rules(rule(outcome="warning"))
        assert cfg.ruleset_hash(True, doc) == cfg.ruleset_hash(True, same) != cfg.ruleset_hash(True, other)
        assert cfg.ruleset_hash(True, doc) != cfg.ruleset_hash(False, doc)

    def test_fingerprint_follows_the_inputs(self):
        a, b = inputs(answers={"Q-101": "no"}), inputs(answers={"Q-101": "no"})
        assert input_fingerprint(a) == input_fingerprint(b) != input_fingerprint(inputs(answers={"Q-101": "yes"}))
        assert input_fingerprint(a) != input_fingerprint(inputs(answers={"Q-101": "no"}, score=final_score()))


def test_explanation_for_an_undecided_rule_names_the_missing_input():
    out = run(rule(condition=leaf("answer", "TEMP", "LESS_THAN", "5")), answers={"TEMP": AnswerState(EMPTY)})
    assert "could not be decided" in out.rules[0]["explanation"] and "not answered" in out.rules[0]["explanation"]


def test_decimal_values_are_formatted_without_noise():
    out = run(rule(condition=leaf("score", "percentage", "LESS_THAN", "70")), score=final_score("62.5000"))
    assert "observed: 62.5)" in out.explanation[0] and Decimal("62.5") == Decimal("62.5000")


# ---------------------------------------------------------------------------------------------------------------- the documented examples (docs/evaluation-example-rule-sets.md)
EXAMPLE_A = [
    {"code": "CRIT", "name": "Critical safety failure", "priority": 10, "outcome": "fail", "message": "Escalate to the QA manager.",
     "condition": {"logic": "OR", "conditions": [leaf("score", "critical", "EQUALS", True), leaf("answer", "Q-101", "EQUALS", "no")]}},
    {"code": "LOW", "name": "Low score review", "priority": 20, "outcome": "review_required",
     "condition": {"logic": "AND", "conditions": [leaf("score", "status", "EQUALS", "final"), leaf("score", "percentage", "LESS_THAN", "70")]}},
]
EXAMPLE_B = [
    {"code": "HOT", "name": "Temperature too high", "priority": 10, "outcome": "fail", "condition": leaf("answer", "TEMP", "GREATER_THAN", "8")},
    {"code": "GRADE", "name": "Grade D needs review", "priority": 20, "outcome": "review_required", "condition": leaf("score", "grade", "EQUALS", "D")},
    {"code": "NOTE", "name": "A leak was mentioned", "priority": 30, "outcome": "warning", "condition": leaf("answer", "NOTE", "CONTAINS", "leak")},
]


def temp(value):
    return registry.get("numeric").parse_operand(value, SNAP.items["TEMP"].context())


class TestDocumentedExamples:
    def test_example_a(self):
        cases = [({"Q-101": "yes"}, final_score("85", "B"), "pass", []), ({"Q-101": "yes"}, final_score("62", "D"), "review_required", ["LOW"]),
                 ({"Q-101": "no"}, final_score("62", "D"), "fail", ["CRIT", "LOW"]), ({"Q-101": "yes"}, final_score("90", "A", critical=True), "fail", ["CRIT"]),
                 ({"Q-101": "yes"}, sources.score_states(None), "pass", [])]
        for answers, score, outcome, matched in cases:
            out = run(*EXAMPLE_A, answers=answers, score=score)
            assert (out.outcome, out.matched) == (outcome, matched)

    def test_example_b(self):
        hot = run(*EXAMPLE_B, answers={"TEMP": temp("9.5"), "NOTE": "small leak"}, score=final_score("75", "C"))
        assert (hot.outcome, hot.matched) == ("fail", ["HOT", "NOTE"])
        cold = run(*EXAMPLE_B, answers={"TEMP": temp("4"), "NOTE": AnswerState(EMPTY)}, score=final_score("55", "D"))
        assert (cold.outcome, cold.matched, cold.undecided) == ("review_required", ["GRADE"], ["NOTE"])
        fine = run(*EXAMPLE_B, answers={"TEMP": temp("4"), "NOTE": "all good"}, score=final_score("95", "A"))
        assert (fine.outcome, fine.matched) == ("pass", [])
