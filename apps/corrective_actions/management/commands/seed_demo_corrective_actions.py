"""Demonstrate corrective actions end to end on generic data (idempotent, development data only).

Builds on ``seed_demo_findings``: for the first rule-raised finding it creates two independent actions (one verified and closed, one
awaiting verification) and a draft; independent verification needs a *second* person, so the demo verifies only when another active
user may verify at the location - it never invents users or credentials, and never lets the owner verify their own work.
"""

from __future__ import annotations

from datetime import timedelta

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from apps.corrective_actions import services
from apps.corrective_actions.models import CorrectiveAction
from apps.executions.management.commands.seed_demo_executions import Command as ExecutionsDemo
from apps.findings.models import Finding
from apps.organizations.models import Organization
from apps.permissions.authorization import has_permission
from apps.permissions.registry import P

TITLE = "Clear and mark the emergency exit"


class Command(BaseCommand):
    help = "Seed the corrective-action demo (actions in several states against a demo finding)."

    def add_arguments(self, parser) -> None:  # noqa: ANN001
        parser.add_argument("--organization", help="Organization slug (default: the only organization).")

    def handle(self, *args, **options) -> None:  # noqa: ANN002, ANN003
        organizations = Organization.objects.all()
        if options["organization"]:
            organizations = organizations.filter(slug=options["organization"])
        if organizations.count() != 1:
            raise CommandError("Name exactly one organization with --organization <slug>.")
        organization = organizations.get()
        call_command("seed_demo_findings", organization=organization.slug, stdout=self.stdout)
        if CorrectiveAction.objects.filter(organization=organization, title=TITLE).exists():
            self.stdout.write("Corrective action demo: already present.")
            return
        actor = ExecutionsDemo()._actor(organization)
        finding = Finding.objects.filter(organization=organization, source_type="checklist_rule").order_by("number").first()
        if finding is None:
            raise CommandError("No rule-raised finding exists; run seed_demo_findings first.")
        verifier = next((u for u in get_user_model().objects.filter(is_active=True, organization_memberships__organization=organization).distinct().exclude(pk=actor.pk)
                         if has_permission(u, P.CORRECTIVE_ACTION_VERIFY, organization=organization, location=finding.location)), None)
        due = timezone.now() + timedelta(days=7)
        done = services.create_action(actor=actor, finding=finding, title=TITLE, description="Remove the stacked boxes and paint the floor marking.",
                                      plan="1. Clear the aisle. 2. Repaint the marking.", completion_criteria="Exit is clear and marked", priority="high",
                                      assigned_to=actor, due_at=due, issue=True)
        services.transition(done, "start", actor=actor)
        done = services.transition(done, "submit", actor=actor, reason="The aisle is clear and the marking repainted.")
        if verifier is not None:
            done = services.transition(done, "verify", actor=verifier, reason="Checked on site.")
            services.transition(done, "close", actor=actor)
            self.stdout.write(f"{done.reference}: verified by {verifier.email} and closed.")
        else:
            self.stdout.write(f"{done.reference}: submitted - it needs a different user with corrective_action.verify to decide (separation of duties).")
        pending = services.create_action(actor=actor, finding=finding, title="Retrain staff on exit clearance", priority="normal", assigned_to=actor, due_at=due, issue=True)
        services.transition(pending, "start", actor=actor)
        services.add_update(pending, actor=actor, body="Training session scheduled for Monday.", progress_percent=30)
        draft = services.create_action(actor=actor, finding=finding, title="Add exit checks to the opening routine", priority="low")
        self.stdout.write(f"{pending.reference} is in progress; {draft.reference} is a draft. {finding.reference} stays open until its actions are done.")
        self.stdout.write(self.style.SUCCESS("Corrective action demo created. Open /corrective-actions/."))
