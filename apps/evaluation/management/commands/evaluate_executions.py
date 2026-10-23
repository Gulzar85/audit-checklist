"""Evaluate submitted executions whose submission-time evaluation did not complete (a defect never loses a submission).

Idempotent: an execution whose inputs, rules and score are unchanged since its current run is left alone (the evaluation reports "unchanged"); running it twice
changes nothing.  Actions are never repeated (their keys are unique once succeeded).
"""

from __future__ import annotations

from django.core.management.base import BaseCommand

from apps.evaluation import services
from apps.evaluation.models import EvaluationRuleSet
from apps.executions.constants import ExecutionStatus
from apps.executions.models import ChecklistExecution


class Command(BaseCommand):
    help = "Create the missing final evaluation of submitted executions (idempotent)."

    def add_arguments(self, parser) -> None:  # noqa: ANN001
        parser.add_argument("--organization", help="Organization slug.")
        parser.add_argument("--dry-run", action="store_true", help="Only report how many executions have no evaluation yet.")

    def handle(self, *args, **options) -> None:  # noqa: ANN002, ANN003
        versions = EvaluationRuleSet.objects.filter(enabled=True).values("version_id")
        pending = (ChecklistExecution.objects.filter(status=ExecutionStatus.SUBMITTED, version_id__in=versions, evaluation_runs__isnull=True)
                   .select_related("organization", "assignment", "version__template").distinct())
        if options["organization"]:
            pending = pending.filter(organization__slug=options["organization"])
        total = pending.count()
        if options["dry_run"]:
            self.stdout.write(f"{total} execution(s) would be evaluated.")
            return
        done = failed = 0
        for execution in pending.iterator():
            try:
                run = services.evaluate_pending(execution)
            except Exception:  # noqa: BLE001 - reported below; the activity log has the details
                run = None
            done, failed = done + (1 if run else 0), failed + (0 if run else 1)
        self.stdout.write(self.style.SUCCESS(f"Evaluated {done} execution(s); {failed} could not be evaluated (see the activity log)."))
