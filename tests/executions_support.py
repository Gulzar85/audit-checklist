"""Builders for the Phase 9 tests: the Phase 8 world plus a checklist that uses many answer types, and executor users."""

from __future__ import annotations

from apps.assignments import services as assignment_services
from apps.checklists.models import ChecklistItem
from apps.checklists.seed import load_template
from apps.permissions import services as rbac
from apps.permissions.models import MembershipRole
from apps.permissions.registry import P

from assignments_support import build_world, future

EXECUTOR = [P.EXECUTION_CREATE, P.EXECUTION_UPDATE, P.EXECUTION_SUBMIT, P.EXECUTION_VIEW, P.LOCATION_VIEW]

CHECKLIST = {
    "code": "EXC", "name": "Execution Test Checklist", "category": "Safety",
    "sections": [
        {"code": "FIRE", "name": "Fire", "items": [
            {"code": "FIRE-EXIT", "question": "Is the fire exit accessible?", "answer_type": "yes_no"},
            {"code": "HANDWASH", "question": "Is the handwashing station operational?", "answer_type": "yes_no_na"},
            {"code": "BOOL", "question": "Is the sign lit?", "answer_type": "boolean", "required": False},
        ]},
        {"code": "FOOD", "name": "Food", "items": [
            {"code": "FRIDGE", "question": "Fridge temperature?", "answer_type": "numeric", "config": {"min": 0, "max": 10, "decimals": 1, "unit": "C"}},
            {"code": "KIND", "question": "Storage kind?", "answer_type": "single_choice", "required": False,
             "options": [["dry", "Dry"], ["cold", "Cold"]]},
            {"code": "TAGS", "question": "Issues seen?", "answer_type": "multiple_choice", "required": False,
             "options": [["pest", "Pests"], ["leak", "Leaks"], ["dirt", "Dirt"]], "config": {"max_select": 2}},
            {"code": "RATING", "question": "Cleanliness rating", "answer_type": "score", "required": False, "config": {"min": 1, "max": 5, "step": 1}},
            {"code": "GRID", "question": "Area condition", "answer_type": "matrix", "required": False,
             "config": {"rows": ["Kitchen", "Dining area"], "columns": ["Clean", "Damaged"]}},
        ]},
        {"code": "GEN", "name": "General", "items": [
            {"code": "ISSUE", "question": "Describe the issue.", "answer_type": "long_text", "required": False},
            {"code": "NOTE", "question": "Any note?", "answer_type": "text", "required": False, "config": {"max_length": 20}},
            {"code": "WHEN", "question": "Inspection date", "answer_type": "date", "required": False},
            {"code": "STAMP", "question": "Inspection time", "answer_type": "datetime", "required": False},
            {"code": "PHOTO", "question": "Photo of the exit", "answer_type": "photo", "required": False},
            {"code": "SIGN", "question": "Signature", "answer_type": "signature", "required": False},
        ]},
    ],
    "rules": [
        {"name": "Blocked exit needs a description", "condition": {"source_item_code": "FIRE-EXIT", "comparison": "EQUALS", "value": "no"},
         "actions": [{"type": "SHOW_ITEM", "target": {"type": "item", "code": "ISSUE"}},
                     {"type": "REQUIRE_ITEM", "target": {"type": "item", "code": "ISSUE"}}]},
        {"name": "A broken sink needs a comment", "condition": {"source_item_code": "HANDWASH", "comparison": "EQUALS", "value": "no"},
         "actions": [{"type": "REQUIRE_COMMENT", "target": {"type": "item", "code": "HANDWASH"}, "prompt": "Why is it not working?"}]},
        {"name": "A lit sign needs a photo of the fridge", "condition": {"source_item_code": "BOOL", "comparison": "EQUALS", "value": True},
         "actions": [{"type": "REQUIRE_EVIDENCE", "target": {"type": "item", "code": "FRIDGE"}, "kinds": ["photo"], "min_count": 1}]},
        {"name": "A blocked exit tightens the note and limits storage", "condition": {"source_item_code": "FIRE-EXIT", "comparison": "EQUALS", "value": "no"},
         "actions": [{"type": "SET_VALIDATION_CONSTRAINT", "target": {"type": "item", "code": "NOTE"}, "constraints": {"max_length": 5}},
                     {"type": "OPTION_AVAILABILITY", "target": {"type": "item", "code": "KIND"}, "mode": "only", "options": ["cold"]}]},
    ],
}

REQUIRED = {"FIRE-EXIT": "yes", "HANDWASH": "yes", "FRIDGE": "4.5"}


def build_exec_world(org_a, org_b, make_user, assign, make_type):
    w = build_world(org_a, org_b, make_user, assign, make_type)
    executor_role = rbac.create_role(organization=org_a, name="Executor", permissions=EXECUTOR)
    runner = make_user(email="ex-runner@example.com", organization=org_a, role=None)  # may run checklists at Restaurant 1
    assign(runner, org_a, executor_role, scope=MembershipRole.ScopeType.LOCATION, location=w["r1"])
    runner2 = make_user(email="ex-runner2@example.com", organization=org_a, role=None)  # also Restaurant 1
    assign(runner2, org_a, executor_role, scope=MembershipRole.ScopeType.LOCATION, location=w["r1"])
    area_runner = make_user(email="ex-area@example.com", organization=org_a, role=None)  # the whole of Area 1
    assign(area_runner, org_a, executor_role, scope=MembershipRole.ScopeType.LOCATION_DESCENDANTS, location=w["area1"])
    w.update(runner=runner, runner2=runner2, area_runner=area_runner)
    template, _created = load_template(org_a, CHECKLIST)
    w["etemplate"] = template
    w["eversion"] = template.versions.get(version_number=1)
    return w


def item_id(version, code):
    return str(ChecklistItem.objects.get(section__version=version, code=code).pk)


def issued(w, location=None, *, assigned_to=None, version=None):
    """An issued assignment of the execution test checklist."""
    return assignment_services.create_assignment(
        actor=w["admin"], version=version or w["eversion"], location=location or w["r1"], assigned_to=assigned_to, due_at=future(48), issue=True)

