"""Idempotently (re)run finding detection for submitted executions.

Findings are raised automatically when an execution is submitted; this command is the safe way to raise anything that was missed
(a recorded ``finding.detection_failed``) or to backfill after a detection rule was added to a version that already has submitted
executions.  Re-running is always safe: a finding's identity is its detection key, so existing findings are returned, never duplicated.
"""

from __future__ import annotations

from datetime import datetime

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from apps.executions.constants import ExecutionStatus
from apps.executions.models import ChecklistExecution
from apps.findings import services


class Command(BaseCommand):
    help = "Raise the findings that submitted executions' rules call for (idempotent)."

    def add_arguments(self, parser) -> None:  # noqa: ANN001
        parser.add_argument("--execution", help="One execution reference (EXE-00001).")
        parser.add_argument("--organization", help="Organization slug.")
        parser.add_argument("--since", help="Only executions submitted on or after this date (YYYY-MM-DD).")
        parser.add_argument("--dry-run", action="store_true", help="Report what would be raised without writing.")

    def handle(self, *args, **options) -> None:  # noqa: ANN002, ANN003
        executions = ChecklistExecution.objects.filter(status=ExecutionStatus.SUBMITTED).select_related(
            "organization", "assignment__location", "version__template", "submitted_by").order_by("submitted_at")
        if options["organization"]:
            executions = executions.filter(organization__slug=options["organization"])
        if options["execution"]:
            ref = options["execution"].upper().removeprefix("EXE-")
            if not ref.isdigit():
                raise CommandError("Use a reference such as EXE-00042.")
            executions = executions.filter(number=int(ref))
        if options["since"]:
            try:
                since = timezone.make_aware(datetime.strptime(options["since"], "%Y-%m-%d"))
            except ValueError as exc:
                raise CommandError("--since must be YYYY-MM-DD.") from exc
            executions = executions.filter(submitted_at__gte=since)
        raised = checked = 0
        for execution in executions.iterator():
            checked += 1
            if options["dry_run"]:
                from apps.executions.services.evaluation import build_state
                from apps.findings.services.detection import collect

                wanted = collect(build_state(execution))
                if wanted:
                    self.stdout.write(f"{execution.reference}: {len(wanted)} finding(s) would be raised or already exist.")
                continue
            outcome = services.detect_for_execution(execution, actor=execution.submitted_by)
            if outcome.created:
                raised += len(outcome.created)
                self.stdout.write(f"{execution.reference}: raised {', '.join(f.reference for f in outcome.created)}.")
            for problem in outcome.problems:
                self.stderr.write(f"{execution.reference}: {problem}")
        self.stdout.write(self.style.SUCCESS(f"Checked {checked} execution(s); raised {raised} finding(s)."))
