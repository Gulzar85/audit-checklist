"""The "Daily Facility Safety Check" demo: every conditional behaviour is configuration, proven with the shared evaluator."""

from __future__ import annotations

import pytest
from django.core.management import call_command

from apps.checklists.models import ChecklistRule, ChecklistTemplate
from apps.checklists.rules import evaluate_snapshot, snapshot_for_version, validate_rule_set


@pytest.fixture
def demo(org_a):
    call_command("seed_demo_checklists", organization=org_a.slug)
    template = ChecklistTemplate.objects.get(organization=org_a, code="DEMO-DFSC")
    version = template.versions.get()
    return template, version, snapshot_for_version(version)


def run(snapshot, **answers):
    return evaluate_snapshot(snapshot, {k.replace("_", "-").upper(): v for k, v in answers.items()})


@pytest.mark.django_db
class TestDailyFacilitySafetyCheck:
    def test_it_is_published_valid_and_generic(self, demo):
        template, version, snapshot = demo
        assert version.status == "published" and template.status == "active"
        assert validate_rule_set(snapshot).ok and not validate_rule_set(snapshot).warnings
        assert [i.answer_type for i in snapshot.items.values()][:6] == ["yes_no", "yes_no", "numeric", "long_text", "photo", "yes_no"]
        assert ChecklistRule.objects.filter(version=version).count() == 4  # no per-question code: just rows of data

    def test_loading_twice_changes_nothing(self, demo, org_a):
        call_command("seed_demo_checklists", organization=org_a.slug)
        assert ChecklistRule.objects.filter(version__template__code="DEMO-DFSC").count() == 4

    def test_follow_up_questions_are_revealed_only_when_the_fire_exit_is_blocked(self, demo):
        _, _, snapshot = demo
        ok = run(snapshot, fire_exit="yes")
        assert not ok.items["FIRE-ISSUE"].visible and not ok.items["FIRE-PHOTO"].visible
        blocked = run(snapshot, fire_exit="no")
        assert blocked.items["FIRE-ISSUE"].visible and blocked.items["FIRE-PHOTO"].visible
        assert blocked.items["FIRE-ISSUE"].required and blocked.items["FIRE-PHOTO"].required
        assert not run(snapshot).items["FIRE-ISSUE"].visible  # not answered yet: stays hidden (UNKNOWN never acts)

    def test_a_comment_is_required_with_the_blocked_answer(self, demo):
        _, _, snapshot = demo
        blocked = run(snapshot, fire_exit="no")
        assert blocked.items["FIRE-EXIT"].comment_required and blocked.items["FIRE-EXIT"].comment_prompts == ["Say what is blocking the exit"]
        assert not run(snapshot, fire_exit="yes").items["FIRE-EXIT"].comment_required

    def test_temperature_warning_with_or_and_a_nested_group(self, demo):
        _, _, snapshot = demo
        assert not run(snapshot, fridge_ok="yes", fridge_temp="3").messages
        assert "outside the safe" in run(snapshot, fridge_ok="no").messages[0]["message"]  # first branch
        hot = run(snapshot, fridge_ok="yes", fridge_temp="9")
        cold = run(snapshot, fridge_ok="yes", fridge_temp="-4")
        assert hot.messages and cold.messages and hot.items["FRIDGE-OK"].comment_required
        assert hot.items["FRIDGE-TEMP"].messages[0]["severity"] == "warning"

    def test_the_section_is_controlled_by_a_boolean_answer(self, demo):
        _, _, snapshot = demo
        assert run(snapshot).sections["EXTRA"]["visible"] is False and not run(snapshot).items["GREASE-TRAP"].visible
        assert run(snapshot, extra_needed="no").sections["EXTRA"]["visible"] is False
        yes = run(snapshot, extra_needed="yes")
        assert yes.sections["EXTRA"]["visible"] is True and yes.items["GREASE-TRAP"].visible and yes.items["GREASE-TRAP"].required

    def test_hidden_answers_are_ignored_not_counted(self, demo):
        _, _, snapshot = demo
        done = run(snapshot, fire_exit="yes", fridge_ok="yes", extra_needed="no", grease_trap="yes", fire_issue="stale text")
        assert done.completion["complete"] and "GREASE-TRAP" not in done.completion["unanswered_required"]
        assert done.items["FIRE-ISSUE"].answer.status == "hidden"

    def test_and_combination_escalates_only_when_both_fail(self, demo):
        _, _, snapshot = demo
        both = run(snapshot, fire_exit="no", fridge_ok="no")
        assert any(m["severity"] == "error" and "escalate" in m["message"].lower() for m in both.messages)
        assert not any(m["severity"] == "error" for m in run(snapshot, fire_exit="no", fridge_ok="yes").messages)

    def test_completion_follows_the_visible_required_questions(self, demo):
        _, _, snapshot = demo
        blocked = run(snapshot, fire_exit="no", fridge_ok="yes", extra_needed="no")
        assert blocked.completion["unanswered_required"] == ["FIRE-ISSUE", "FIRE-PHOTO"] and not blocked.completion["complete"]
