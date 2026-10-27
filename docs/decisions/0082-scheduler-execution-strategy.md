# 0082. Scheduler execution strategy: a DB-backed queue run by a management command

- **Status:** Accepted
- **Date:** 2026-10-27

## Context
Django 6.1 has `django.tasks` but only Immediate/Dummy backends and no worker; Redis, Celery and brokers are excluded; the deployment is Windows with an OS scheduler available. Phase 17 left bounded, idempotent entry points for the scheduler.

## Decision
Jobs live in PostgreSQL (`ScheduledJob`); `manage.py process_scheduled_jobs` (run by Task Scheduler/cron every minute, or `--loop`) recovers expired leases, materializes due schedule occurrences and runs due jobs. A schedule is **one row** (`ScheduleDefinition`) that creates a job only when an occurrence is due, under a unique idempotency key, so future occurrences are never pre-created. An optional Django Tasks adapter (`JOBS_EXECUTION_BACKEND=django_tasks`) can hand manual run-now jobs to a backend that has a real worker; it is off by default and does not change the protocol. Recurrence is five structured shapes over `zoneinfo`; **no cron parser** and no new dependency.

## Consequences
No new infrastructure; latency up to the polling interval; execution is at-least-once, so handlers must be idempotent; the application is decoupled from the runner (any process that calls `run_once` works).
