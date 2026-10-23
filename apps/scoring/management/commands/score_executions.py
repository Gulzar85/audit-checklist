"""Score submitted executions whose submission-time scoring did not complete (a scoring defect never loses a submission).

Idempotent: an execution that already has a current result is skipped; running it twice changes nothing.  Final results are only ever
*created* here - never recalculated (that is an authorized, audited action).
"""

from __future__ import annotations

from django.core.management.base import BaseCommand

from apps.executions.constants import ExecutionStatus
from apps.executions.models import ChecklistExecution
from apps.scoring import services
from apps.scoring.models import ScoringConfig


class Command(BaseCommand):
    help = "Create the missing final score of submitted executions (idempotent)."

    def add_arguments(self, parser) -> None:  # noqa: ANN001
        parser.add_argument("--organization", help="Organization slug.")
        parser.add_argument("--dry-run", action="store_true", help="Only report how many executions would be scored.")

    def handle(self, *args, **options) -> None:  # noqa: ANN002, ANN003
        scored_versions = ScoringConfig.objects.filter(enabled=True).values("version_id")
        pending = (ChecklistExecution.objects.filter(status=ExecutionStatus.SUBMITTED, version_id__in=scored_versions, score_results__isnull=True)
                   .select_related("organization", "assignment", "version__template", "submitted_by").distinct())
        if options["organization"]:
            pending = pending.filter(organization__slug=options["organization"])
        total = pending.count()
        if options["dry_run"]:
            self.stdout.write(f"{total} execution(s) would be scored.")
            return
        done = failed = 0
        for execution in pending.iterator():
            result = services.score_pending(execution)
            done, failed = done + (1 if result else 0), failed + (0 if result else 1)
        self.stdout.write(self.style.SUCCESS(f"Scored {done} execution(s); {failed} could not be scored (see the activity log)."))
