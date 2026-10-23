"""Builders for the Phase 15 tests: the execution world plus scoring users and a published, scored checklist version."""

from __future__ import annotations

from apps.checklists import lifecycle
from apps.checklists.seed import load_template
from apps.executions import services as exec_services
from apps.permissions import services as rbac
from apps.permissions.models import MembershipRole
from apps.permissions.registry import P
from apps.scoring import services as scoring

from executions_support import build_exec_world, issued, item_id

BANDS = [{"min": "0", "grade": "D", "label": "Needs improvement"}, {"min": "70", "grade": "C", "label": "Satisfactory"},
         {"min": "80", "grade": "B", "label": "Good"}, {"min": "90", "grade": "A", "label": "Excellent"}]

#: Scoring of the execution-test checklist: FIRE-EXIT (weight 2) + HANDWASH + FRIDGE, a critical rule on a blocked fire exit.
EXC_DOC = {
    "items": {
        "FIRE-EXIT": {"weight": "2", "points": {"yes": "5", "no": "0"}},
        "HANDWASH": {"points": {"yes": "5", "no": "0"}},
        "FRIDGE": {"ranges": [{"min": "0", "max": "4", "max_inclusive": False, "points": "0"}, {"min": "4", "max": "10", "points": "5"}]},
    },
    "bands": BANDS,
    "critical": [{"item": "FIRE-EXIT", "op": "equals", "value": "no", "label": "Fire exit blocked"}],
}

COND_CHECKLIST = {
    "code": "CND", "name": "Conditional Scoring Checklist", "category": "Safety",
    "sections": [{"code": "MAIN", "name": "Main", "items": [
        {"code": "A", "question": "Is A fine?", "answer_type": "yes_no"},
        {"code": "B", "question": "Is B fine?", "answer_type": "yes_no"},
    ]}],
    "rules": [{"name": "B is not needed when A is fine", "condition": {"source_item_code": "A", "comparison": "EQUALS", "value": "yes"},
               "actions": [{"type": "HIDE_ITEM", "target": {"type": "item", "code": "B"}}]}],
}
COND_DOC = {"items": {"A": {"points": {"yes": "1", "no": "0"}}, "B": {"points": {"yes": "1", "no": "0"}}}}


def build_scoring_world(org_a, org_b, make_user, assign, make_type):
    w = build_exec_world(org_a, org_b, make_user, assign, make_type)
    org_role = rbac.create_role(organization=org_a, name="Scoring designer", permissions=[P.CHECKLIST_VIEW, P.SCORING_VIEW, P.SCORING_CONFIGURE])
    w["designer"] = make_user(email="sc-designer@example.com", organization=org_a, role=None)
    assign(w["designer"], org_a, org_role)
    for key, perms in (("score_viewer", [P.EXECUTION_VIEW, P.LOCATION_VIEW, P.SCORING_SCORE_VIEW]),
                       ("breakdown_viewer", [P.EXECUTION_VIEW, P.LOCATION_VIEW, P.SCORING_SCORE_VIEW, P.SCORING_BREAKDOWN_VIEW]),
                       ("previewer", [P.EXECUTION_VIEW, P.LOCATION_VIEW, P.SCORING_SCORE_VIEW, P.SCORING_RECALCULATE]),
                       ("recalculator", [P.EXECUTION_VIEW, P.LOCATION_VIEW, P.SCORING_SCORE_VIEW, P.SCORING_BREAKDOWN_VIEW, P.SCORING_FINALIZE_RECALCULATE]),
                       ("exec_viewer", [P.EXECUTION_VIEW, P.LOCATION_VIEW])):
        role = rbac.create_role(organization=org_a, name=f"Scoring {key}", permissions=perms)
        user = make_user(email=f"sc-{key}@example.com", organization=org_a, role=None)
        assign(user, org_a, role, scope=MembershipRole.ScopeType.LOCATION, location=w["r1"])
        w[key] = user
    return w


def publish_scored(w, template="etemplate", document=None, settings=None, summary="scoring"):
    """A new published version of ``w[template]`` carrying the given scoring (default: the execution-checklist scoring)."""
    draft = lifecycle.create_draft(w[template], actor=w["admin"], change_summary=summary)
    scoring.save_config(draft, actor=w["admin"], settings=settings, document=EXC_DOC if document is None else document)
    lifecycle.publish_version(draft, actor=w["admin"], expected_hash=lifecycle.run_validation(draft).draft_hash)
    draft.refresh_from_db()
    return draft


def answers_for(version, values):
    return [exec_services.AnswerInput(item_id(version, code), value) for code, value in values.items()]


def run(w, version, values, *, who="runner", submit=True, location=None):
    """Start an execution of ``version``, answer, and (by default) submit; returns the execution."""
    execution, _created = exec_services.start_or_resume(issued(w, location, version=version), actor=w[who])
    if values:
        exec_services.save_answers(execution, actor=w[who], inputs=answers_for(version, values))
    if submit:
        return exec_services.submit(execution, actor=w[who]).execution
    return execution


GOOD = {"FIRE-EXIT": "yes", "HANDWASH": "yes", "FRIDGE": "4.5"}
