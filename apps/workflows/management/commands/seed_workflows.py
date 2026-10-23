"""Seed ready-made workflows for one organization (idempotent, development / onboarding data).

* ``ca-verification`` - *corrective action verification*: starts automatically when a corrective action is submitted, creates one claimable task for holders of
  ``corrective_action.verify`` (never the action's owner or the submitter), and on approval / rejection invokes the Corrective Action services.  It is the organization's
  default workflow for corrective actions.
* ``critical-finding-review`` - *conditional approval*: a finding of high severity (rank >= 30) goes to a reviewer holding ``finding.resolve`` who acknowledges or stops it;
  other findings complete without a review step.

Both are created and published through the real services as the organization's first administrator-capable user.
"""

from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from apps.organizations.models import Organization
from apps.workflows import services as wf
from apps.workflows.models import WorkflowDefinition

CA_STATES = [
    {"code": "awaiting", "name": "Awaiting verification", "kind": "initial", "assignee_policy": {"type": "permission", "permission": "corrective_action.verify"},
     "separation": ["not_target_owner", "not_starter"], "due_hours": 72, "on_no_assignee": "attention"},
    {"code": "verified", "name": "Verified", "kind": "success"},
    {"code": "returned", "name": "Returned for correction", "kind": "failure"},
]
CA_TRANSITIONS = [
    {"source": "awaiting", "target": "verified", "action": "approve", "label": "Approve", "decision": "approve", "handler": "corrective_action.verify"},
    {"source": "awaiting", "target": "returned", "action": "reject", "label": "Reject", "decision": "reject", "handler": "corrective_action.reject", "reason_required": True},
]
FINDING_STATES = [
    {"code": "intake", "name": "Intake", "kind": "initial"},
    {"code": "review", "name": "Senior review", "kind": "intermediate", "assignee_policy": {"type": "permission", "permission": "finding.resolve"}, "due_hours": 24,
     "on_no_assignee": "attention"},
    {"code": "approved", "name": "Cleared", "kind": "success"},
    {"code": "stopped", "name": "Stopped", "kind": "failure"},
]
FINDING_TRANSITIONS = [
    {"source": "intake", "target": "review", "action": "submit", "label": "Send for review", "actor_rule": "authorized", "sort_order": 10,
     "condition": {"attribute": "severity_rank", "operator": "GREATER_THAN_OR_EQUAL", "value": 30}},
    {"source": "intake", "target": "approved", "action": "submit", "label": "Send for review", "actor_rule": "authorized", "sort_order": 20},
    {"source": "review", "target": "approved", "action": "approve", "label": "Approve", "decision": "approve", "handler": "finding.acknowledge"},
    {"source": "review", "target": "stopped", "action": "reject", "label": "Reject", "decision": "reject", "reason_required": True},
]


class Command(BaseCommand):
    help = "Seed the corrective-action verification and critical-finding review workflows for one organization."

    def add_arguments(self, parser) -> None:  # noqa: ANN001
        parser.add_argument("--organization", help="Organization slug (default: the only organization).")

    def handle(self, *args, **options) -> None:  # noqa: ANN002, ANN003
        organizations = Organization.objects.all()
        if options["organization"]:
            organizations = organizations.filter(slug=options["organization"])
        if organizations.count() != 1:
            raise CommandError("Name exactly one organization with --organization <slug>.")
        organization = organizations.get()
        actor = self._actor(organization)
        for code, name, target, states, transitions, extra in (
                ("ca-verification", "Corrective action verification", "corrective_action", CA_STATES, CA_TRANSITIONS, {"is_default": True, "auto_start_event": "corrective_action.submitted"}),
                ("critical-finding-review", "Critical finding review", "finding", FINDING_STATES, FINDING_TRANSITIONS, {})):
            if WorkflowDefinition.objects.filter(organization=organization, code=code).exists():
                self.stdout.write(f"{code}: already present.")
                continue
            definition = wf.create_definition(actor=actor, organization=organization, code=code, name=name, target_type=target, description=name, **extra)
            draft = definition.versions.get()
            for s in states:
                wf.add_state(draft, actor=actor, **s)
            for t in transitions:
                wf.add_transition(draft, actor=actor, **t)
            wf.publish(draft, actor=actor)
            self.stdout.write(f"{code}: created and published (version 1).")
        self.stdout.write(self.style.SUCCESS("Workflows ready. Open /workflows/."))

    def _actor(self, organization: Organization):  # noqa: ANN202
        from django.contrib.auth import get_user_model

        from apps.permissions.authorization import has_permission
        from apps.permissions.registry import P

        needed = (P.WORKFLOW_CREATE, P.WORKFLOW_UPDATE_DRAFT, P.WORKFLOW_PUBLISH)
        for user in get_user_model().objects.filter(is_active=True, organization_memberships__organization=organization).distinct().order_by("date_joined"):
            if all(has_permission(user, code, organization=organization) for code in needed):
                return user
        raise CommandError("No active user in this organization may design workflows; give someone the Administrator role first.")
