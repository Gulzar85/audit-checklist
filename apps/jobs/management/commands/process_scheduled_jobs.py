"""The production runner: one bounded pass (cron / Task Scheduler) or a polling loop.

    manage.py process_scheduled_jobs                    # one pass, then exit (schedule it every minute)
    manage.py process_scheduled_jobs --loop             # keep polling until SIGINT / Ctrl+C / SIGTERM
    manage.py process_scheduled_jobs --dry-run          # report what is due; change nothing

Exit codes: 0 = the pass completed (individual jobs may have failed - that is recorded on them, not an operational failure); 2 = the runner itself failed (database, configuration).
Several runners may run at once and overlapping invocations are safe: jobs are claimed with row locks and leases.
"""

from __future__ import annotations

import signal
import sys
import threading
import time

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from apps.jobs import constants as k
from apps.jobs import services
from apps.jobs.models import ScheduleDefinition, ScheduledJob


class Command(BaseCommand):
    help = "Materialize due schedules and run due jobs (bounded, idempotent, concurrency-safe)."

    def add_arguments(self, parser) -> None:  # noqa: ANN001
        parser.add_argument("--limit", type=int, default=20, help=f"Maximum jobs per pass (1-{k.MAX_BATCH}).")
        parser.add_argument("--max-seconds", type=int, default=240, help=f"Stop starting jobs after this many seconds (1-{k.MAX_RUNTIME_SECONDS}).")
        parser.add_argument("--loop", action="store_true", help="Poll repeatedly instead of exiting after one pass.")
        parser.add_argument("--poll-interval", type=int, default=30, help="Seconds to wait between passes with --loop (5-3600); the wait is longer when the queue is empty.")
        parser.add_argument("--runner-id", default="", help="A name for this runner in diagnostics (default: host:pid).")
        parser.add_argument("--dry-run", action="store_true", help="Only report what is due.")

    def handle(self, *args, **options) -> None:  # noqa: ANN002, ANN003
        limit, max_seconds, poll = options["limit"], options["max_seconds"], options["poll_interval"]
        if not 1 <= limit <= k.MAX_BATCH or not 1 <= max_seconds <= k.MAX_RUNTIME_SECONDS or not 5 <= poll <= 3600:
            raise CommandError("--limit, --max-seconds or --poll-interval is out of range.")
        if options["dry_run"]:
            now = timezone.now()
            due = ScheduleDefinition.objects.filter(status=k.ScheduleStatus.ACTIVE, next_run_at__lte=now).count()
            ready = ScheduledJob.objects.filter(status__in=[k.JobStatus.PENDING, k.JobStatus.RETRY_PENDING], available_at__lte=now).count()
            expired = ScheduledJob.objects.filter(status__in=[k.JobStatus.CLAIMED, k.JobStatus.RUNNING], lease_expires_at__lt=now).count()
            self.stdout.write(f"Dry run: {due} schedule(s) due, {ready} job(s) ready, {expired} expired lease(s). Nothing was changed.")
            return
        stop = threading.Event()
        for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
            sig = getattr(signal, name, None)
            if sig is not None:
                try:
                    signal.signal(sig, lambda *_: stop.set())
                except (ValueError, OSError):  # not the main thread / unsupported here
                    pass
        try:
            while True:
                summary = services.run_once(limit=limit, max_seconds=max_seconds, runner=options["runner_id"], stop=stop.is_set)
                self.stdout.write(f"pass: ran={summary['ran']} succeeded={summary['succeeded']} retry={summary['retry']} failed={summary['failed']} lost={summary['lost']} "
                                  f"created={summary['materialized']['created']} recovered={summary['recovered']}")
                if not options["loop"] or stop.is_set():
                    break
                waited = 0
                pause = poll if summary["ran"] else min(poll * 2, 300)  # an empty queue is polled less eagerly
                while waited < pause and not stop.is_set():
                    time.sleep(1)
                    waited += 1
                if stop.is_set():
                    break
        except Exception as exc:  # noqa: BLE001 - operational failure of the runner itself
            self.stderr.write(f"The runner failed: {type(exc).__name__}")
            sys.exit(2)
