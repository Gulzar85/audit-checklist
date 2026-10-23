"""The scoring calculation and configuration validation (Phase 15) - pure unit tests, no database."""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace as NS

import pytest

from apps.scoring import config as cfg
from apps.scoring import constants as k
from apps.scoring.engine import ItemFacts, calculate, input_fingerprint, quantize

D = Decimal


def opt(value, active=True):
    return NS(value=value, label=value.title(), is_active=active)


def spec(code, atype, section="S-001", options=(), active=True, **config):
    return NS(code=code, answer_type=atype, section_code=section, options=tuple(options), active=active, config=config, question=f"Question {code}")


def snapshot(*items, sections=("S-001", "S-002")):
    return NS(items={i.code: i for i in items}, sections={s: NS(name=f"Section {s}") for s in sections})


def settings(**over):
    return {**cfg.default_settings(), **over}


def build(document, **over):
    return cfg.parse(settings(**over), {**cfg.empty_document(), **document})


def fact(code, atype, status="answered", value=None, section="S-001", visible=True, **config):
    return ItemFacts(code, section, f"Question {code}", atype, config, visible, status, value)


YES_NO = {"weight": "1", "points": {"yes": "1", "no": "0"}}


# ---------------------------------------------------------------------------
# Question-level mapping
# ---------------------------------------------------------------------------
class TestQuestionScoring:
    def test_boolean_mapping_is_configurable_not_assumed(self):
        data = build({"items": {"B": {"points": {"true": "0", "false": "4"}}}})  # "No" is the good answer here
        out = calculate(data, [fact("B", "boolean", value=False)], final=True)
        assert (out.earned, out.available, out.percentage) == (D(4), D(4), D(100))

    def test_yes_no_and_na_is_governed_by_policy(self):
        doc = {"items": {"A": {"points": {"yes": "5", "no": "0"}}, "B": {"points": {"yes": "5", "no": "0"}}}}
        facts = [fact("A", "yes_no_na", value="yes"), fact("B", "yes_no_na", value="na")]
        excluded = calculate(build(doc, na_policy="exclude"), facts, final=True)
        assert (excluded.earned, excluded.available, excluded.percentage) == (D(5), D(5), D(100))
        zero = calculate(build(doc, na_policy="zero"), facts, final=True)
        assert (zero.earned, zero.available, zero.percentage) == (D(5), D(10), D(50))

    def test_item_level_not_applicable_follows_the_same_policy(self):
        doc = {"items": {"A": {"points": {"yes": "2", "no": "0"}}, "B": {"points": {"yes": "2", "no": "0"}}}}
        facts = [fact("A", "yes_no", value="no"), fact("B", "yes_no", status="not_applicable")]
        assert calculate(build(doc, na_policy="exclude"), facts, final=True).available == D(2)
        assert calculate(build(doc, na_policy="zero"), facts, final=True).available == D(4)

    def test_single_choice_maps_each_option(self):
        doc = {"items": {"Q": {"points": {"excellent": "5", "good": "4", "poor": "1"}}}}
        out = calculate(build(doc), [fact("Q", "single_choice", value="good")], final=True)
        assert (out.earned, out.available, out.percentage) == (D(4), D(5), D(80))

    @pytest.mark.parametrize("aggregate,expected_earned,expected_available", [("sum", D(5), D(6)), ("max", D(3), D(3))])
    def test_multiple_choice_uses_the_selected_aggregate(self, aggregate, expected_earned, expected_available):
        doc = {"items": {"Q": {"aggregate": aggregate, "points": {"a": "1", "b": "2", "c": "3"}}}}
        out = calculate(build(doc), [fact("Q", "multiple_choice", value=["b", "c"])], final=True)
        assert (out.earned, out.available) == (expected_earned, expected_available)

    def test_multiple_choice_with_nothing_selected_earns_zero_of_its_maximum(self):
        doc = {"items": {"Q": {"points": {"a": "1", "b": "2"}}}}
        out = calculate(build(doc), [fact("Q", "multiple_choice", value=[])], final=True)
        assert (out.earned, out.available, out.percentage) == (D(0), D(3), D(0))

    @pytest.mark.parametrize("value,points", [("95", D(5)), ("90", D(5)), ("89.99", D(4)), ("75", D(4)), ("50", D(2)), ("49.99", D(0)), ("0", D(0))])
    def test_numeric_ranges_with_explicit_boundaries(self, value, points):
        ranges = [{"min": "90", "max": "100", "points": "5"}, {"min": "75", "max": "90", "max_inclusive": False, "points": "4"},
                  {"min": "50", "max": "75", "max_inclusive": False, "points": "2"}, {"min": "0", "max": "50", "max_inclusive": False, "points": "0"}]
        out = calculate(build({"items": {"N": {"ranges": ranges}}}), [fact("N", "numeric", value=value, min=0, max=100)], final=True)
        assert out.items[0].points == points and out.items[0].available == D(5)

    def test_numeric_value_outside_every_range_is_excluded_flagged_and_never_zero(self):
        ranges = [{"min": "10", "max": "20", "points": "3"}]
        out = calculate(build({"items": {"N": {"ranges": ranges}, "Y": YES_NO}}),
                        [fact("N", "numeric", value="5"), fact("Y", "yes_no", value="yes")], final=True)
        item = out.items[0]
        assert (item.included, item.reason) == (False, "no_matching_range")
        assert out.warnings and out.status == k.ResultStatus.PROVISIONAL  # not certifiable as final
        assert out.available == D(1)

    def test_score_answer_is_the_score_and_its_type_maximum_is_the_denominator(self):
        out = calculate(build({"items": {"S": {"weight": "1"}}}), [fact("S", "score", value=4, min=1, max=5)], final=True)
        assert (out.earned, out.available, out.percentage) == (D(4), D(5), D(80))

    def test_unmapped_answer_is_excluded_not_zeroed(self):
        out = calculate(build({"items": {"Q": {"points": {"a": "1"}}}}), [fact("Q", "single_choice", value="zzz")], final=True)
        assert out.items[0].included is False and out.status == k.ResultStatus.NOT_APPLICABLE

    def test_non_scored_types_are_not_scored(self):
        out = calculate(build({"items": {"Y": YES_NO}}), [fact("Y", "yes_no", value="yes"), fact("T", "text", value="free text")], final=True)
        assert [i.code for i in out.items] == ["Y"]


# ---------------------------------------------------------------------------
# Weights, aggregation, rounding
# ---------------------------------------------------------------------------
class TestAggregation:
    def test_question_weights(self):
        doc = {"items": {"A": {"weight": "2", "points": {"yes": "5", "no": "0"}}, "B": {"weight": "1", "points": {"yes": "5", "no": "0"}}}}
        out = calculate(build(doc), [fact("A", "yes_no", value="yes"), fact("B", "yes_no", value="no")], final=True)
        assert (out.earned, out.available) == (D(10), D(15))
        assert out.percentage == D("66.67")
        assert "weight 2" in out.items[0].explanation

    def test_zero_weight_question_contributes_nothing_but_is_listed(self):
        doc = {"items": {"A": {"weight": "0", "points": {"yes": "5", "no": "0"}}, "B": YES_NO}}
        out = calculate(build(doc), [fact("A", "yes_no", value="no"), fact("B", "yes_no", value="yes")], final=True)
        assert (out.earned, out.available, out.percentage) == (D(1), D(1), D(100))
        assert len(out.items) == 2

    def test_weighted_sections_weights_apply_between_sections_only(self):
        doc = {"items": {"A": {"weight": "3", "points": {"yes": "1", "no": "0"}}, "B": {"weight": "1", "points": {"yes": "1", "no": "0"}},
                         "C": YES_NO}, "sections": {"S-001": "30", "S-002": "70"}}
        facts = [fact("A", "yes_no", value="yes"), fact("B", "yes_no", value="no"), fact("C", "yes_no", value="no", section="S-002")]
        out = calculate(build(doc, method="weighted_sections"), facts, final=True)
        # S-001: 3/4 = 75 %, S-002: 0 % -> (75 x 30 + 0 x 70) / 100 = 22.5
        assert out.percentage == D("22.50")
        assert [s.percentage for s in out.sections] == [D("75.00"), D("0.00")]

    def test_weighted_sections_skips_sections_without_available_points(self):
        doc = {"items": {"A": YES_NO, "C": YES_NO}, "sections": {"S-001": "1", "S-002": "1"}}
        facts = [fact("A", "yes_no", value="yes"), fact("C", "yes_no", status="not_applicable", section="S-002")]
        assert calculate(build(doc, method="weighted_sections"), facts, final=True).percentage == D("100.00")

    def test_raw_total_reports_points_and_percentage(self):
        out = calculate(build({"items": {"A": YES_NO}}, method="raw_total"), [fact("A", "yes_no", value="yes")], final=True)
        assert (out.method, out.earned, out.percentage) == ("raw_total", D(1), D(100))

    @pytest.mark.parametrize("precision,expected", [(0, "67"), (1, "66.7"), (2, "66.67"), (4, "66.6667")])
    def test_rounding_precision_is_exact_half_up(self, precision, expected):
        doc = {"items": {"A": YES_NO, "B": YES_NO, "C": YES_NO}}
        facts = [fact("A", "yes_no", value="yes"), fact("B", "yes_no", value="yes"), fact("C", "yes_no", value="no")]
        assert str(calculate(build(doc, precision=precision), facts, final=True).percentage) == expected

    def test_half_up_not_bankers_rounding(self):
        assert quantize(D("0.125"), 2) == D("0.13") and quantize(D("0.135"), 2) == D("0.14")

    def test_no_binary_float_drift(self):
        doc = {"items": {c: {"points": {"yes": "0.1", "no": "0"}} for c in "ABC"}}
        facts = [fact(c, "yes_no", value="yes") for c in "ABC"]
        out = calculate(build(doc), facts, final=True)
        assert out.earned == D("0.3") and out.percentage == D("100.00")

    def test_deterministic(self):
        doc = {"items": {"A": YES_NO, "B": YES_NO}}
        facts = [fact("A", "yes_no", value="yes"), fact("B", "yes_no", value="no")]
        first = calculate(build(doc), facts, final=True).as_dict()
        shuffled = calculate(build(doc), list(reversed(facts)), final=True).as_dict()  # same numbers whatever the input order; items follow the input order
        assert {k_: v for k_, v in first.items() if k_ != "items"} == {k_: v for k_, v in shuffled.items() if k_ != "items"}
        assert sorted(first["items"], key=lambda i: i["code"]) == sorted(shuffled["items"], key=lambda i: i["code"])
        assert first == calculate(build(doc), facts, final=True).as_dict()
        assert input_fingerprint(facts) == input_fingerprint(list(reversed(facts)))


# ---------------------------------------------------------------------------
# Missing answers, hidden questions, no denominator
# ---------------------------------------------------------------------------
class TestInclusion:
    DOC = {"items": {"A": YES_NO, "B": YES_NO}}

    def test_a_draft_excludes_unanswered_and_is_provisional(self):
        out = calculate(build(self.DOC), [fact("A", "yes_no", value="yes"), fact("B", "yes_no", status="empty")], final=False)
        assert (out.status, out.percentage, out.available) == ("provisional", D(100), D(1))

    @pytest.mark.parametrize("policy,expected", [("zero", D(50)), ("exclude", D(100))])
    def test_missing_policy_applies_only_to_final(self, policy, expected):
        out = calculate(build(self.DOC, missing_policy=policy), [fact("A", "yes_no", value="yes"), fact("B", "yes_no", status="empty")], final=True)
        assert (out.status, out.percentage) == ("final", expected)

    def test_hidden_questions_are_never_penalized(self):
        out = calculate(build(self.DOC), [fact("A", "yes_no", value="yes"), fact("B", "yes_no", status="hidden", visible=False)], final=True)
        assert out.percentage == D(100) and out.items[1].reason == "hidden"

    def test_a_hidden_answer_that_still_has_a_value_is_ignored(self):
        out = calculate(build(self.DOC), [fact("A", "yes_no", value="yes"), fact("B", "yes_no", value="no", visible=False)], final=True)
        assert out.percentage == D(100)

    def test_invalid_answers_are_excluded_and_flagged(self):
        out = calculate(build(self.DOC), [fact("A", "yes_no", value="yes"), fact("B", "yes_no", status="invalid")], final=True)
        assert out.items[1].reason == "invalid" and out.warnings and out.status == "provisional"

    def test_nothing_to_score_is_not_applicable_never_zero_percent(self):
        out = calculate(build(self.DOC), [fact("A", "yes_no", status="not_applicable"), fact("B", "yes_no", status="hidden", visible=False)], final=True)
        assert (out.status, out.percentage, out.earned, out.grade) == ("not_applicable", None, None, "")

    def test_zero_denominator_is_not_a_division_by_zero(self):
        out = calculate(build({"items": {"A": {"weight": "0", "points": {"yes": "1", "no": "0"}}}}), [fact("A", "yes_no", value="yes")], final=True)
        assert out.status == "not_applicable" and out.percentage is None


# ---------------------------------------------------------------------------
# Grades and critical failures
# ---------------------------------------------------------------------------
BANDS = [{"min": "0", "grade": "D", "label": "Needs improvement"}, {"min": "70", "grade": "C", "label": "Satisfactory"},
         {"min": "80", "grade": "B", "label": "Good"}, {"min": "90", "grade": "A", "label": "Excellent"}]


class TestGradesAndCritical:
    @pytest.mark.parametrize("yes,total,grade", [(10, 10, "A"), (9, 10, "A"), (8, 10, "B"), (7, 10, "C"), (6, 10, "D"), (0, 10, "D")])
    def test_lower_bound_is_inclusive_and_resolution_deterministic(self, yes, total, grade):
        doc = {"items": {f"Q{i}": YES_NO for i in range(total)}, "bands": BANDS}
        facts = [fact(f"Q{i}", "yes_no", value="yes" if i < yes else "no") for i in range(total)]
        assert calculate(build(doc), facts, final=True).grade == grade

    def test_the_grade_never_replaces_the_number(self):
        doc = {"items": {"A": YES_NO}, "bands": BANDS}
        out = calculate(build(doc), [fact("A", "yes_no", value="yes")], final=True)
        assert (out.percentage, out.grade, out.label) == (D(100), "A", "Excellent")

    def test_no_grade_when_not_applicable(self):
        out = calculate(build({"items": {"A": YES_NO}, "bands": BANDS}), [fact("A", "yes_no", status="not_applicable")], final=True)
        assert out.grade == "" and out.display_label == ""

    CRIT = [{"item": "A", "op": "equals", "value": "no", "label": "Fire exit blocked"}]

    def test_critical_failure_is_a_flag_beside_the_unchanged_score(self):
        doc = {"items": {"A": YES_NO, "B": YES_NO}, "bands": BANDS, "critical": self.CRIT}
        out = calculate(build(doc), [fact("A", "yes_no", value="no"), fact("B", "yes_no", value="yes")], final=True)
        assert out.critical and out.critical_hits[0]["label"] == "Fire exit blocked"
        assert (out.percentage, out.grade, out.display_label) == (D(50), "D", "Needs improvement")

    def test_critical_failure_can_replace_the_displayed_outcome_only(self):
        doc = {"items": {"A": YES_NO, "B": YES_NO}, "bands": BANDS, "critical": self.CRIT}
        out = calculate(build(doc, critical_overrides_grade=True, critical_label="FAIL"),
                        [fact("A", "yes_no", value="no"), fact("B", "yes_no", value="yes")], final=True)
        assert (out.display_label, out.grade, out.percentage) == ("FAIL", "D", D(50))

    def test_no_critical_flag_when_the_answer_is_fine_or_the_question_is_hidden(self):
        doc = {"items": {"A": YES_NO, "B": YES_NO}, "critical": self.CRIT}
        assert not calculate(build(doc), [fact("A", "yes_no", value="yes"), fact("B", "yes_no", value="yes")], final=True).critical
        assert not calculate(build(doc), [fact("A", "yes_no", value="no", visible=False), fact("B", "yes_no", value="yes")], final=True).critical

    def test_points_below_and_multi_choice_equals(self):
        doc = {"items": {"A": {"points": {"yes": "5", "no": "0"}}, "M": {"points": {"x": "1", "y": "1"}}},
               "critical": [{"item": "A", "op": "points_below", "value": "3", "label": "low"}, {"item": "M", "op": "equals", "value": "y", "label": "has y"}]}
        out = calculate(build(doc), [fact("A", "yes_no", value="no"), fact("M", "multiple_choice", value=["x", "y"])], final=True)
        assert {h["label"] for h in out.critical_hits} == {"low", "has y"}


# ---------------------------------------------------------------------------
# Validation of a configuration against the real questions
# ---------------------------------------------------------------------------
def issues(document, snap, **over):
    return cfg.validate(settings(**over), {**cfg.empty_document(), **document}, snap)


def codes(found, level="error"):
    return {i.code for i in found if i.level == level}


class TestValidation:
    SNAP = snapshot(spec("YN", "yes_no"), spec("YNA", "yes_no_na"), spec("BOOL", "boolean"), spec("TXT", "text"),
                    spec("SC", "single_choice", options=[opt("a"), opt("b"), opt("old", active=False)]),
                    spec("MC", "multiple_choice", options=[opt("a"), opt("b")]),
                    spec("NUM", "numeric", min=0, max=100), spec("SCORE", "score", min=1, max=5), spec("SCORE0", "score", min=0, max=0))

    def test_a_complete_configuration_is_valid(self):
        doc = {"items": {"YN": YES_NO, "SC": {"points": {"a": "1", "b": "2", "old": "0"}}, "SCORE": {}}, "bands": BANDS}
        assert codes(issues(doc, self.SNAP)) == set()

    def test_unsupported_answer_type(self):
        assert "unsupported_type" in codes(issues({"items": {"TXT": {"points": {"x": "1"}}}}, self.SNAP))

    def test_unknown_item_and_section(self):
        found = issues({"items": {"NOPE": YES_NO}, "sections": {"S-999": "1"}}, self.SNAP)
        assert {"unknown_item", "unknown_section"} <= codes(found)

    def test_scoring_enabled_without_any_scored_question(self):
        assert "no_scored_items" in codes(issues({}, self.SNAP))

    def test_disabled_scoring_needs_nothing(self):
        assert issues({}, self.SNAP, enabled=False) == []

    def test_fixed_type_mappings_must_be_complete_and_known(self):
        assert "missing_mapping" in codes(issues({"items": {"YN": {"points": {"yes": "1"}}}}, self.SNAP))
        assert "unknown_option" in codes(issues({"items": {"YN": {"points": {"yes": "1", "no": "0", "maybe": "1"}}}}, self.SNAP))
        assert "unknown_option" in codes(issues({"items": {"YNA": {"points": {"yes": "1", "no": "0", "na": "0"}}}}, self.SNAP))  # N/A is policy, not points

    def test_every_active_option_needs_points_and_stale_options_are_refused(self):
        assert "missing_mapping" in codes(issues({"items": {"SC": {"points": {"a": "1"}}}}, self.SNAP))
        assert "unknown_option" in codes(issues({"items": {"SC": {"points": {"a": "1", "b": "1", "zzz": "1"}}}}, self.SNAP))

    @pytest.mark.parametrize("weight", ["-1", "10001"])
    def test_weights_must_be_in_range(self, weight):
        assert "invalid_weight" in codes(issues({"items": {"YN": {**YES_NO, "weight": weight}}}, self.SNAP))

    def test_negative_points_are_refused_no_penalties(self):
        assert "invalid_points" in codes(issues({"items": {"YN": {"points": {"yes": "1", "no": "-1"}}}}, self.SNAP))

    @pytest.mark.parametrize("bad", [1.5, True, "abc", "NaN", "Infinity", None])
    def test_malformed_numbers_never_parse(self, bad):
        found = issues({"items": {"YN": {"weight": bad, "points": {"yes": "1", "no": "0"}}}}, self.SNAP)
        assert codes(found) == {"malformed"}

    def test_numeric_overlap_is_an_error(self):
        ranges = [{"min": "0", "max": "50", "points": "1"}, {"min": "50", "max": "100", "points": "2"}]  # both inclusive at 50
        assert "overlapping_ranges" in codes(issues({"items": {"NUM": {"ranges": ranges}}}, self.SNAP))

    def test_numeric_adjacent_exclusive_boundary_is_valid(self):
        ranges = [{"min": "0", "max": "50", "max_inclusive": False, "points": "1"}, {"min": "50", "max": "100", "points": "2"}]
        assert codes(issues({"items": {"NUM": {"ranges": ranges}}}, self.SNAP)) == set()

    def test_numeric_gap_in_a_bounded_question_is_an_error(self):
        ranges = [{"min": "0", "max": "40", "points": "1"}, {"min": "60", "max": "100", "points": "2"}]
        assert "range_gap" in codes(issues({"items": {"NUM": {"ranges": ranges}}}, self.SNAP))

    def test_numeric_not_covering_the_ends_is_a_gap(self):
        ranges = [{"min": "10", "max": "100", "points": "1"}]
        assert "range_gap" in codes(issues({"items": {"NUM": {"ranges": ranges}}}, self.SNAP))

    def test_score_question_takes_no_mapping_and_needs_a_maximum(self):
        assert "unexpected_mapping" in codes(issues({"items": {"SCORE": {"points": {"1": "1"}}}}, self.SNAP))
        assert "no_maximum" in codes(issues({"items": {"SCORE0": {}}}, self.SNAP))

    def test_multi_choice_aggregate_must_be_known(self):
        assert "invalid_aggregate" in codes(issues({"items": {"MC": {"aggregate": "avg", "points": {"a": "1", "b": "1"}}}}, self.SNAP))

    def test_bands_must_cover_from_zero_without_duplicates(self):
        base = {"items": {"YN": YES_NO}}
        assert "bands_do_not_cover" in codes(issues({**base, "bands": [{"min": "50", "grade": "A", "label": ""}]}, self.SNAP))
        assert "duplicate_band" in codes(issues({**base, "bands": [{"min": "0", "grade": "A", "label": ""}, {"min": "0", "grade": "B", "label": ""}]}, self.SNAP))
        assert "invalid_band" in codes(issues({**base, "bands": [{"min": "0", "grade": "A", "label": ""}, {"min": "101", "grade": "B", "label": ""}]}, self.SNAP))
        assert "no_bands" in codes(issues(base, self.SNAP), "warning")

    def test_critical_rules_are_checked_against_the_question(self):
        base = {"items": {"YN": YES_NO}}
        ok = {"item": "YN", "op": "equals", "value": "no", "label": "x"}
        assert codes(issues({**base, "critical": [ok]}, self.SNAP)) == set()
        assert "invalid_critical_value" in codes(issues({**base, "critical": [{**ok, "value": "maybe"}]}, self.SNAP))
        assert "critical_item_not_scored" in codes(issues({**base, "critical": [{**ok, "item": "BOOL"}]}, self.SNAP))
        assert "unknown_item" in codes(issues({**base, "critical": [{**ok, "item": "NOPE"}]}, self.SNAP))
        assert "invalid_critical_op" in codes(issues({**base, "critical": [{**ok, "op": "matches"}]}, self.SNAP))

    def test_override_needs_a_label(self):
        assert "missing_critical_label" in codes(issues({"items": {"YN": YES_NO}}, self.SNAP, critical_overrides_grade=True))

    @pytest.mark.parametrize("over", [{"method": "magic"}, {"na_policy": "x"}, {"missing_policy": "x"}, {"precision": 9}, {"precision": True}])
    def test_settings_are_validated(self, over):
        assert codes(issues({"items": {"YN": YES_NO}}, self.SNAP, **over))

    def test_unknown_document_keys_are_refused(self):
        assert codes(cfg.validate(settings(), {"items": {}, "eval": "1+1"}, self.SNAP)) == {"malformed"}

    def test_weighted_sections_need_a_positive_weight_somewhere(self):
        doc = {"items": {"YN": YES_NO}, "sections": {"S-001": "0"}}
        assert "no_section_weight" in codes(issues(doc, self.SNAP, method="weighted_sections"))


class TestCanonicalForm:
    def test_equivalent_input_hashes_identically(self):
        a = cfg.to_document(build({"items": {"A": {"weight": "1.0", "points": {"yes": "1.00", "no": "0"}}}}))
        b = cfg.to_document(build({"items": {"A": {"weight": "1", "points": {"no": "0", "yes": "1"}}}}))
        assert a == b and cfg.config_hash(settings(), a) == cfg.config_hash(settings(), b)

    def test_any_change_changes_the_hash(self):
        a = cfg.to_document(build({"items": {"A": YES_NO}}))
        b = cfg.to_document(build({"items": {"A": {"weight": "2", "points": {"yes": "1", "no": "0"}}}}))
        assert cfg.config_hash(settings(), a) != cfg.config_hash(settings(), b)
        assert cfg.config_hash(settings(), a) != cfg.config_hash(settings(precision=3), a)

    def test_round_trip_is_stable(self):
        doc = cfg.to_document(build({"items": {"A": YES_NO}, "bands": BANDS}))
        assert cfg.to_document(cfg.parse(settings(), doc)) == doc
