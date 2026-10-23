"""Builders for the Phase 14 tests: the corrective-action world plus workflow designers, approvers and ready-made workflows."""

from __future__ import annotations

from apps.corrective_actions import services as ca_services
from apps.permissions import services as rbac
from apps.permissions.models import MembershipRole
from apps.permissions.registry import P
from apps.workflows import services as wf

from actions_support import build_actions_world, make_action, make_finding, to_status

DESIGNER = [P.WORKFLOW_VIEW, P.WORKFLOW_CREATE, P.WORKFLOW_UPDATE_DRAFT, P.WORKFLOW_VALIDATE, P.WORKFLOW_PUBLISH, P.WORKFLOW_ARCHIVE, P.LOCATION_VIEW]
RUNTIME = [P.WORKFLOW_INSTANCE_VIEW, P.WORKFLOW_TASK_VIEW, P.WORKFLOW_TASK_CLAIM, P.WORKFLOW_TRANSITION, P.LOCATION_VIEW]
STARTER = [P.WORKFLOW_START, P.WORKFLOW_INSTANCE_VIEW, P.LOCATION_VIEW]
MANAGER = [P.WORKFLOW_INSTANCE_VIEW, P.WORKFLOW_TASK_VIEW, P.WORKFLOW_TASK_REASSIGN, P.WORKFLOW_CANCEL, P.WORKFLOW_START, P.LOCATION_VIEW]


def build_workflow_world(org_a, org_b, make_user, assign, make_type):
    w = build_actions_world(org_a, org_b, make_user, assign, make_type)
    for name, perms, who, org_wide in (("Workflow designer", DESIGNER, "designer", True), ("Workflow runtime", RUNTIME, "wf_runner", False), ("Workflow starter", STARTER, "wf_starter", False),
                                       ("Workflow manager", MANAGER, "wf_manager", False)):
        role = rbac.create_role(organization=org_a, name=name, permissions=perms)
        user = make_user(email=f"wf-{who}@example.com", organization=org_a, role=None)
        if org_wide:
            assign(user, org_a, role)
        else:
            assign(user, org_a, role, scope=MembershipRole.ScopeType.LOCATION, location=w["r1"])
        w[who] = user
    # the verifier also works through workflow tasks (claim / view), as the seeded Auditor role does
    runtime = rbac.create_role(organization=org_a, name="Workflow task worker", permissions=RUNTIME)
    assign(w["verifier"], org_a, runtime, scope=MembershipRole.ScopeType.LOCATION, location=w["r1"])
    return w


def build(w, *, code="demo", target_type="corrective_action", organization=None, states, transitions, default=False, auto="", allow_loops=False, start_condition=None, publish=True):
    """Create a definition through the real services.  ``states``: dicts for ``add_state``; ``transitions``: dicts for ``add_transition``."""
    org = organization or w["org_a"]
    definition = wf.create_definition(actor=w["admin"], organization=org, code=code, name=f"Workflow {code}", target_type=target_type, is_default=default, auto_start_event=auto)
    draft = definition.versions.get()
    if start_condition is not None or allow_loops:
        wf.update_version(draft, actor=w["admin"], start_condition=start_condition, allow_loops=allow_loops)
    for s in states:
        wf.add_state(draft, actor=w["admin"], **s)
    for t in transitions:
        wf.add_transition(draft, actor=w["admin"], **t)
    if publish:
        wf.publish(draft, actor=w["admin"])
    return definition


#: The Example A workflow: a submitted corrective action is verified by someone other than its owner / submitter.
CA_STATES = [
    {"code": "awaiting", "name": "Awaiting verification", "kind": "initial", "assignee_policy": {"type": "permission", "permission": "corrective_action.verify"},
     "separation": ["not_target_owner", "not_starter"], "due_hours": 48},
    {"code": "verified", "name": "Verified", "kind": "success"},
    {"code": "returned", "name": "Returned for correction", "kind": "failure"},
]
CA_TRANSITIONS = [
    {"source": "awaiting", "target": "verified", "action": "approve", "label": "Approve", "decision": "approve", "handler": "corrective_action.verify"},
    {"source": "awaiting", "target": "returned", "action": "reject", "label": "Reject", "decision": "reject", "handler": "corrective_action.reject", "reason_required": True},
]


def ca_workflow(w, **kw):
    return build(w, code="ca-verification", states=CA_STATES, transitions=CA_TRANSITIONS, default=True, **kw)


def submitted_action(w, **kw):
    """A corrective action in ``submitted`` status (owner worker1, submitted by worker1)."""
    return to_status(w, make_action(w, **kw), "submitted")


_ = (ca_services, make_finding)
