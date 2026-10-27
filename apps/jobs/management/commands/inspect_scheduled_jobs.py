"""A read-only health report of scheduling (the same data as the diagnostics screen).  Exit code 1 when something needs attention (no recent runner, expired leases, failures)."""

from __future__ import annotations

from django.core.management.base import BaseCommand

from apps.jobs import selectors


class Command(BaseCommand):
    help = "Show runner activity, backlog, expired leases and repeated failures."

    def handle(self, *args, **options) -> None:  # noqa: ANN002, ANN003
        d = selectors.diagnostics()
        self.stdout.write(f"Runner: {d['runner_state']} (last seen: {d['last_seen_at'].isoformat() if d['last_seen_at'] else 'never'}; {d['last_runner'] or '-'})")
        self.stdout.write(f"Ready now: {d['waiting_now']} (waiting over the window: {d['waiting_overdue']}); retry pending: {d['retry_pending']}; expired leases: {d['expired_leases']}; overdue schedules: {d['overdue_schedules']}")
        for row in d["repeated_failures"]:
            self.stdout.write(f"Repeated failures: {row['task_type']} x{row['n']} in the last day")
        bad = d["runner_state"] != "alive" and (d["waiting_now"] or d["overdue_schedules"]) or d["expired_leases"] or d["repeated_failures"]
        if d["runner_state"] == "never_seen":
            self.stderr.write(self.style.WARNING("No runner has ever reported in: nothing is processing jobs. See docs/scheduling-runner-and-operations.md."))
        if bad:
            raise SystemExit(1)
