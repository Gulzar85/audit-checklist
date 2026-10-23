"""Builders for the Phase 16 tests: the scoring world plus evaluation users, finding reference data, an execution workflow and published rule sets."""

from __future__ import annotations

from apps.checklists import lifecycle
from apps.evaluation import services as evaluation
from apps.findings.services import ensure_defaults
from apps.permissions import services as rbac
from apps.permissions.models import MembershipRole
from apps.permissions.registry import P
from apps.scoring import services as scoring
from apps.workflows import services as wf

from scoring_support import EXC_DOC, GOOD, build_scoring_world, run

#: Rule sets of the execution-test checklist.  The critical rule has priority 10, so it is evaluated (and wins conflicts) before the score rule.
RULES = {"rules": [
    {"code": "CRIT", "name": "Critical safety failure", "priority": 10, "outcome": "fail", "message": "Escalate to the QA manager.",
     "condition": {"logic": "OR", "conditions": [{"source": "score", "field": "critical", "operator": "EQUALS", "value": True},
                                                  {"source": "answer", "field": "FIRE-EXIT", "operator": "EQUALS", "value": "no"}]},
     "actions": [{"type": "request_finding", "category": "safety", "severity": "high", "title": "Critical safety failure", "description": "Raised by an evaluation rule.",
                  "requires_review": True},
                 {"type": "start_workflow", "workflow": "exec-review"}]},
    {"code": "LOW", "name": "Low score review", "priority": 20, "outcome": "review_required",
     "condition": {"logic": "AND", "conditions": [{"source": "score", "field": "status", "operator": "EQUALS", "value": "final"},
                                                   {"source": "score", "field": "percentage", "operator": "LESS_THAN", "value": "80"}]},
     "actions": [{"type": "request_finding", "category": "quality", "severity": "medium", "title": "Score below target"}, {"type": "tag", "tag": "low-score"}]},
    {"code": "NOSCORE", "name": "Score unavailable", "priority": 30, "outcome": "warning",
     "condition": {"source": "score", "field": "status", "operator": "EQUALS", "value": "unavailable"}},
]}

EVALUATOR_PERMS = {"result_viewer": [P.EXECUTION_VIEW, P.LOCATION_VIEW, P.EVALUATION_RESULT_VIEW],
                   "explainer": [P.EXECUTION_VIEW, P.LOCATION_VIEW, P.EVALUATION_RESULT_VIEW, P.EVALUATION_EXPLAIN],
                   "evaluator": [P.EXECUTION_VIEW, P.LOCATION_VIEW, P.EVALUATION_RESULT_VIEW, P.EVALUATION_EXPLAIN, P.EVALUATION_EVALUATE],
                   "reevaluator": [P.EXECUTION_VIEW, P.LOCATION_VIEW, P.EVALUATION_RESULT_VIEW, P.EVALUATION_EXPLAIN, P.EVALUATION_REEVALUATE],
                   "no_eval": [P.EXECUTION_VIEW, P.LOCATION_VIEW]}


def build_evaluation_world(org_a, org_b, make_user, assign, make_type):
    w = build_scoring_world(org_a, org_b, make_user, assign, make_type)
    org_role = rbac.create_role(organization=org_a, name="Evaluation designer", permissions=[P.CHECKLIST_VIEW, P.EVALUATION_VIEW, P.EVALUATION_CONFIGURE])
    w["rule_designer"] = make_user(email="ev-designer@example.com", organization=org_a, role=None)
    assign(w["rule_designer"], org_a, org_role)
    for key, perms in EVALUATOR_PERMS.items():
        role = rbac.create_role(organization=org_a, name=f"Evaluation {key}", permissions=perms)
        user = make_user(email=f"ev-{key}@example.com", organization=org_a, role=None)
        assign(user, org_a, role, scope=MembershipRole.ScopeType.LOCATION, location=w["r1"])
        w[key] = user
    ensure_defaults(org_a)
    ensure_defaults(org_b)
    w["review_workflow"] = execution_workflow(w)
    return w


def execution_workflow(w, code="exec-review"):
    """A published workflow for checklist executions: one review task for people who can see the execution, completed by a person."""
    definition = wf.create_definition(actor=w["admin"], organization=w["org"], code=code, name="Execution review", target_type="execution")
    draft = definition.versions.get()
    wf.add_state(draft, actor=w["admin"], code="review", name="Review", kind="initial", assignee_policy={"type": "permission", "permission": "execution.view"})
    wf.add_state(draft, actor=w["admin"], code="done", name="Done", kind="success")
    wf.add_transition(draft, actor=w["admin"], source="review", target="done", action="complete", label="Complete", decision="approve")
    wf.publish(draft, actor=w["admin"])
    return definition


def publish_evaluated(w, rules=None, *, template="etemplate", scoring_document=None, enabled=True, summary="evaluation"):
    """A new published version of ``w[template]`` carrying scoring (default: the execution-checklist scoring) and the given evaluation rules."""
    draft = lifecycle.create_draft(w[template], actor=w["admin"], change_summary=summary)
    if scoring_document is not False:
        scoring.save_config(draft, actor=w["admin"], document=EXC_DOC if scoring_document is None else scoring_document)
    evaluation.save_config(draft, actor=w["admin"], enabled=enabled, document=RULES if rules is None else rules)
    lifecycle.publish_version(draft, actor=w["admin"], expected_hash=lifecycle.run_validation(draft).draft_hash)
    draft.refresh_from_db()
    return draft


def rules_with(*codes, **patch):
    """A copy of ``RULES`` keeping only ``codes``, with ``patch`` merged into each kept rule."""
    import copy

    out = copy.deepcopy(RULES)
    out["rules"] = [{**r, **patch} for r in out["rules"] if not codes or r["code"] in codes]
    return out


FIRE_NO = {"FIRE-EXIT": "no", "ISSUE": "Blocked by crates"}
__all__ = ["EXC_DOC", "FIRE_NO", "GOOD", "RULES", "build_evaluation_world", "execution_workflow", "publish_evaluated", "rules_with", "run"]
