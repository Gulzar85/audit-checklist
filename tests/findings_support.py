"""Builders for the Phase 12 tests: the execution world plus a checklist whose rules raise findings, finding users and helpers."""

from __future__ import annotations

from apps.checklists.seed import load_template
from apps.executions import services as exec_services
from apps.findings import services
from apps.permissions import services as rbac
from apps.permissions.models import MembershipRole
from apps.permissions.registry import P

from executions_support import build_exec_world, issued, item_id

TRIAGE = [P.FINDING_VIEW, P.FINDING_ACKNOWLEDGE, P.FINDING_RESOLVE, P.LOCATION_VIEW]
VIEW_ONLY = [P.FINDING_VIEW, P.LOCATION_VIEW]
RAISER = [P.FINDING_VIEW, P.FINDING_CREATE, P.LOCATION_VIEW]
EDITOR = [P.FINDING_VIEW, P.FINDING_UPDATE, P.FINDING_ASSIGN, P.FINDING_CLOSE, P.FINDING_REOPEN, P.FINDING_CANCEL, P.FINDING_ACKNOWLEDGE, P.FINDING_RESOLVE, P.LOCATION_VIEW]


def finding(code, name, **kw):
    return {"type": "RAISE_FINDING", "target": {"type": "item", "code": code}, **kw}


FINDINGS_CHECKLIST = {
    "code": "FND", "name": "Findings Checklist", "category": "Safety",
    "sections": [
        {"code": "S1", "name": "Inspection", "items": [
            {"code": "EXIT", "question": "Is the emergency exit accessible?", "answer_type": "yes_no"},
            {"code": "HAND", "question": "Is the handwashing station operational?", "answer_type": "yes_no_na", "required": False},
            {"code": "RATING", "question": "Cleanliness rating", "answer_type": "score", "required": False, "config": {"min": 1, "max": 5, "step": 1}},
            {"code": "TAGS", "question": "Issues seen?", "answer_type": "multiple_choice", "required": False,
             "options": [["pest", "Pests"], ["leak", "Leaks"], ["dirt", "Dirt"]], "config": {"max_select": 2}},
            {"code": "FRIDGE", "question": "Fridge temperature", "answer_type": "numeric", "required": False, "config": {"min": 0, "max": 20, "decimals": 1}},
            {"code": "ISSUE", "question": "Describe the problem", "answer_type": "long_text", "required": False},
            {"code": "PHOTO", "question": "Photo of the damage", "answer_type": "photo", "required": False,
             "config": {"max_files": 2, "max_size_mb": 5, "allowed_extensions": ["jpg", "png"]}},
        ]},
    ],
    "rules": [
        {"name": "Blocked exit", "condition": {"source_item_code": "EXIT", "comparison": "EQUALS", "value": "no"},
         "actions": [finding("EXIT", "x", category="safety", severity="high", title="Blocked: {question}", description="Answered {answer} at {location}.")]},
        {"name": "Low rating", "condition": {"source_item_code": "RATING", "comparison": "LESS_THAN", "value": 3},
         "actions": [finding("RATING", "x", category="quality", severity="medium")]},
        {"name": "Pests", "condition": {"source_item_code": "TAGS", "comparison": "CONTAINS_ANY", "value": ["pest"]},
         "actions": [finding("TAGS", "x", category="safety", severity="critical", requires_review=True)]},
        {"name": "Handwash broken", "condition": {"source_item_code": "HAND", "comparison": "EQUALS", "value": "no"},
         "actions": [finding("HAND", "x", category="no-such-category", severity="no-such-severity")]},
        {"name": "Follow-up shown", "condition": {"source_item_code": "EXIT", "comparison": "EQUALS", "value": "no"},
         "actions": [{"type": "SHOW_ITEM", "target": {"type": "item", "code": "ISSUE"}}]},
        {"name": "Problem described", "condition": {"source_item_code": "ISSUE", "comparison": "IS_NOT_EMPTY"},
         "actions": [finding("ISSUE", "x", category="process", severity="low")]},
        {"name": "Leak damage", "condition": {"source_item_code": "TAGS", "comparison": "CONTAINS_ANY", "value": ["leak"]},
         "actions": [finding("PHOTO", "x", category="equipment", severity="high", title="Leak damage: {question}")]},
        {"name": "Hot fridge", "condition": {"source_item_code": "FRIDGE", "comparison": "GREATER_THAN", "value": 8},
         "actions": [finding("FRIDGE", "x", category="equipment", severity="high", inherit_evidence=False)]},
    ],
}


def build_findings_world(org_a, org_b, make_user, assign, make_type):
    w = build_exec_world(org_a, org_b, make_user, assign, make_type)
    template, _created = load_template(org_a, FINDINGS_CHECKLIST)
    w["ftemplate"] = template
    w["fversion"] = template.versions.get(version_number=1)
    for name, perms, who in (("Triager", TRIAGE, "triager"), ("Viewer", VIEW_ONLY, "viewer"), ("Raiser", RAISER, "raiser"), ("Editor", EDITOR, "editor")):
        role = rbac.create_role(organization=org_a, name=name, permissions=perms)
        user = make_user(email=f"f-{who}@example.com", organization=org_a, role=None)
        assign(user, org_a, role, scope=MembershipRole.ScopeType.LOCATION, location=w["r1"])
        w[who] = user
    other = rbac.create_role(organization=org_a, name="Elsewhere", permissions=[P.FINDING_VIEW, P.LOCATION_VIEW])
    w["r2_viewer"] = make_user(email="f-r2@example.com", organization=org_a, role=None)
    assign(w["r2_viewer"], org_a, other, scope=MembershipRole.ScopeType.LOCATION, location=w["r2"])
    w["org_a"], w["org_b"] = org_a, org_b
    services.ensure_defaults(org_a)
    return w


def fid(w, code):
    return item_id(w["fversion"], code)


def submit(w, answers, *, location=None, who="runner", **kw):
    """Start an execution of the findings checklist, save ``answers`` ({code: value}) and submit it; returns the submitted execution."""
    ex, _created = exec_services.start_or_resume(issued(w, location, version=w["fversion"], **kw), actor=w[who])
    inputs = [exec_services.AnswerInput(fid(w, c), v) for c, v in answers.items()]
    exec_services.save_answers(ex, actor=w[who], inputs=inputs)
    result = exec_services.submit(ex, actor=w[who])
    return result.execution


def raised(execution):
    from apps.findings.models import Finding

    return {f.rule_code: f for f in Finding.objects.filter(execution=execution)}
