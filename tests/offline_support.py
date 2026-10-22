"""Builders for the Phase 10 tests: the execution world plus a checklist that is fully supported offline."""

from __future__ import annotations

import uuid

from apps.checklists.seed import load_template

from executions_support import build_exec_world, issued, item_id

OFFLINE_CHECKLIST = {
    "code": "OFL", "name": "Offline Checklist", "category": "Safety",
    "sections": [
        {"code": "FIRE", "name": "Fire", "items": [
            {"code": "FIRE-EXIT", "question": "Is the fire exit accessible?", "answer_type": "yes_no"},
            {"code": "HANDWASH", "question": "Is the handwashing station operational?", "answer_type": "yes_no_na"},
        ]},
        {"code": "FOOD", "name": "Food", "items": [
            {"code": "FRIDGE", "question": "Fridge temperature?", "answer_type": "numeric", "config": {"min": 0, "max": 10, "decimals": 1, "unit": "C"}},
            {"code": "TAGS", "question": "Issues seen?", "answer_type": "multiple_choice", "required": False,
             "options": [["pest", "Pests"], ["leak", "Leaks"]]},
            {"code": "PHOTO", "question": "Photo of the exit", "answer_type": "photo", "required": False},
        ]},
        {"code": "GEN", "name": "General", "items": [
            {"code": "ISSUE", "question": "Describe the issue.", "answer_type": "long_text", "required": False},
        ]},
    ],
    "rules": [
        {"name": "Blocked exit needs a description", "condition": {"source_item_code": "FIRE-EXIT", "comparison": "EQUALS", "value": "no"},
         "actions": [{"type": "SHOW_ITEM", "target": {"type": "item", "code": "ISSUE"}}, {"type": "REQUIRE_ITEM", "target": {"type": "item", "code": "ISSUE"}}]},
    ],
}


def build_offline_world(org_a, org_b, make_user, assign, make_type):
    w = build_exec_world(org_a, org_b, make_user, assign, make_type)
    template, _ = load_template(org_a, OFFLINE_CHECKLIST)
    w["otemplate"] = template
    w["oversion"] = template.versions.get(version_number=1)
    return w


def oissued(w, location=None, **kw):
    return issued(w, location, version=w["oversion"], **kw)


def oid(w, code):
    return item_id(w["oversion"], code)


def op(w, assignment, code=None, value=None, *, type="upsert_response", seq=0, base=0, **extra):
    """A well-formed operation with a fresh operation id."""
    out = {"operation_id": str(uuid.uuid4()), "type": type, "seq": seq, "assignment_id": str(assignment.pk), "version_id": str(w["oversion"].pk),
           "schema_version": 1}
    if type != "submit":
        out.update(item_id=oid(w, code), value=value, base_revision=base, not_applicable=False)
    out.update(extra)
    return out
