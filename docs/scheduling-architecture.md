# Scheduling architecture (Phase 18)

A DB-backed scheduler inside the Django monolith (`apps/jobs`). No Redis, Celery, broker or microservice.

## Repository findings (audit)
* **Django 6.1.2 / Python 3.14 / PostgreSQL.** `django.tasks` exists but ships only the *Immediate* and *Dummy* backends - **there is no production worker**. `TASKS` was configured with the Immediate backend (development/tests). No `croniter`, `dateutil` or other scheduling library is installed.
* **Nothing scheduled existed.** Phase 17 deliberately left `process_pending`, `dispatch_batch`, `emit_due_reminders` and `purge` as bounded, idempotent entry points "for Phase 18".
* **Reused:** `apps.notifications.services` (above), `apps.core.services.log_activity` (audit), simple-history, the RBAC registry/`has_permission`, `get_active_organization`, the CBV/DRF conventions, the read-only admin pattern, the default DRF throttle.
* **Gaps (reported, not invented):** assignments and workflow tasks have due dates (`ChecklistAssignment.due_at`, `WorkflowTask.due_at`) but the notification registry has **no reminder events** for them (only `corrective_action.due_soon/overdue`). Registering those tasks would mean inventing lifecycle events (a Phase 17 change), so `checklists.generate_due_reminders` / `workflow_tasks.generate_pending_reminders` are **not registered**. Evaluation follow-up reminders likewise have no existing business rule.

## Execution strategy (selected)
**A management command invoked by the operating-system scheduler** - `manage.py process_scheduled_jobs` - run every minute (Windows Task Scheduler on this deployment, cron/systemd elsewhere), or as a polling loop (`--loop`). It is the simplest option that needs no new infrastructure and works where persistent workers are not available. Trade-off: latency up to one polling interval; see `docs/scheduling-runner-and-operations.md`.
An optional adapter (`apps/jobs/tasks.py`, `JOBS_EXECUTION_BACKEND=django_tasks`) hands *manual run-now* jobs to a Django Tasks backend **that has a real worker**; the job row stays the source of truth and the same lease protocol applies. It is off by default and is not a worker by itself.

## Data model (why each model exists)
| Model | Why |
|---|---|
| `ScheduleDefinition` | one row per *rule* (type, time zone, recurrence, misfire policy, payload, `next_run_at`). Never one row per future occurrence. `organization` null = platform-wide (superusers only). History via simple-history; `revision` counts edits. |
| `ScheduledJob` | one concrete execution request: payload, status, `scheduled_for`, `available_at` (also the retry time), attempts, `idempotency_key` (unique), `correlation_id`, lease (`lease_token`, `lease_expires_at`, `claimed_by`), result summary, sanitized error, cancellation facts. |
| `JobExecution` | one row per attempt (outcome, duration, sanitized error, retry decision, correlation id). |
| `RunnerHeartbeat` | the last time each runner looked at the queue, so "no runner" differs from "idle queue". |
Database constraints: valid statuses/types/policies, unique idempotency key, unique schedule name per organization (and for platform-wide), `next_run_at` only when active, end after start, attempts within bounds, a lease present whenever a job is claimed/running. Indexes: claim (`status, available_at, -priority`), lease, organization listing, schedule listing, schedule due (`status, next_run_at`), retention (`finished_at`).
The payload is validated against a per-task schema, never trusted for organization or user; `max_concurrent` is fixed at **1 per schedule** (an open job of the schedule makes the next occurrence a recorded *skip*), so there is no field for it.

## Task registry (`apps/jobs/registry.py`, `handlers.py`)
A task = stable code, name, description, scope (`global` | `organization` | `both`), strict payload schema (`Field`: bounded int / bool / choice; unknown keys are errors), required permission, schedulable/manual flags, retry policy (max attempts, base and maximum delay), a soft time limit and a handler that calls an **existing service**. A job stores a *code*, never a function path; the runner looks the code up again at execution (an unknown code fails permanently). Registered: `notifications.process_pending`, `notifications.dispatch_retries`, `notifications.purge_history`, `corrective_actions.generate_due_reminders`, `jobs.cleanup_old_execution_history`. Platform-wide tasks can only be created/run by superusers; `corrective_actions.generate_due_reminders` may run for one organization (RBAC `jobs.run`) or, for superusers, all.
Adding a task: write a handler `def handler(ctx: Context) -> dict[str, int|str|bool]`, register a `Task(...)` in `handlers.py`, add tests. Handlers must be **idempotent** (execution is at-least-once), obtain the organization from `ctx.organization` (the job row) and call scoped services; they raise `RetryableError` (temporary), `PermanentError` (no retry) - anything else is treated as a defect.

## Job lifecycle
`pending -> claimed -> running -> succeeded`; `running -> retry_pending -> claimed ...`; `-> failed` (permanent or exhausted); `pending|retry_pending|claimed -> cancelled`; `-> skipped` (schedule stopped, missed run skipped by policy, overlap); `failed -> pending` (manual retry). `succeeded` means *the handler completed its contract* - it never means an email arrived. A **running job cannot be cancelled** (the update is conditional on the status): it finishes, which is safe because handlers are idempotent. A job cancelled while merely claimed never starts (`begin` checks status and lease).

## Concurrency, leases and idempotency
* **Materialization** locks each due schedule (`SELECT ... FOR UPDATE SKIP LOCKED`), creates the occurrence's job under the unique key `sched:<schedule>:<occurrence>` and advances `next_run_at` in the same transaction - an occurrence cannot be created twice.
* **Claiming** locks eligible jobs with `SKIP LOCKED` (priority, then time) and stamps a lease (random token + expiry, `JOBS_LEASE_SECONDS`). Runners claim different rows. The claim transaction ends before anything runs.
* **Start and finish are conditional updates** on `(pk, status, lease_token)`. A runner whose lease was recovered (or whose job was cancelled) matches no row and changes nothing - it can never overwrite a newer attempt.
* **No transaction is held while a handler runs.** Each state change is its own short transaction.
* **Recovery:** expired leases are taken back by `recover_expired` at the start of every pass: a never-started job returns to the queue; a running one records a `lease_expired` attempt and retries (with backoff) or fails.
* At-least-once: a crash after a handler's side effect but before the result is recorded re-runs it; every handler delegates to idempotent services (Phase 17 keys events by `(organization, dedupe_key)`).

## Retries
Retryable failures (`RetryableError`, database/OS/timeout errors) back off `base x 2^(attempt-1)`, capped by the task and by 6 hours, with +-`JOBS_RETRY_JITTER` (default 10%), up to the task's bounded `max_attempts` (<= 10); then `failed` ("retries exhausted"). Permanent errors, unknown tasks and unexpected exceptions fail at once. Only sanitized text is stored: for unexpected exceptions **only the class name** (no traceback, no values). Manual retry (`jobs.run`, reason required) re-queues a *failed* job with a fresh allowance.

## Recurrence and time zones
Five validated shapes, **no cron parser**: `one_time`, `interval` (every N minutes, 5 to 10080, anchored on the start), `daily`, `weekly` (weekdays), `monthly` (day 1-31; a day the month lacks runs on the month's last day). Times are stored in UTC with the IANA zone stored explicitly; daily/weekly/monthly follow the **local wall clock**, so a 09:00 New York schedule stays 09:00 across daylight saving. A local time that does not exist (spring-forward gap) runs shifted forward to when the clock jumps; an ambiguous time (autumn overlap) runs once, at its first occurrence. One-time schedules must be in the future (60 s tolerance). Changing the time zone of a schedule needs an explicit confirmation (`allow_timezone_change`), is audited, and recomputes the next run from *now*.
Edit, enable and resume compute the next run **from now**: nothing is back-filled and no run is duplicated.
**Misfires** (the runner was down): a run more than `JOBS_MISFIRE_GRACE_SECONDS` late is, by policy, **skipped** (one skipped job is recorded, audited) or **run once** to catch up (a single job for the earliest missed occurrence); either way the schedule jumps to the next future occurrence.

## Authorization
Organization schedules/jobs need the organization-wide `jobs.view` / `jobs.manage` / `jobs.run` (administrators have them; QA managers too). Platform-wide schedules, platform-wide tasks and diagnostics are for **superusers only** - an organization admin must not be able to run something that touches other tenants. A task may demand a stricter permission. Handlers run as the **system** (no user) and receive their organization from the job row. Every query is scoped to the caller's organizations; an invisible object is a 404.

## Audit and logging
`ActivityLog` verbs `jobs.*`: schedule created/updated/enabled/disabled/paused/resumed, manual run, cancelled, manual retry, failed, retry scheduled, retries exhausted, lease recovered, misfire, history purged - each with actor, organization and correlation id. **Successful** runs and the claim/start steps are not written to the activity log (a 5-minute schedule would add ~600 rows a day); they are recorded in `JobExecution` and as structured log lines (`apps.jobs` logger: job id, task, organization, attempt, correlation id, duration, status). Payload values, tokens and exception text are never logged.

## Retention
`jobs.cleanup_old_execution_history` deletes finished jobs (and, by cascade, their attempts) older than `JOBS_HISTORY_RETENTION_DAYS` (90, minimum 7), bounded per run, and prunes heartbeats older than 30 days. Open jobs are never deleted.

## Known limitations
No second-level latency (polling), no kill of a running handler (soft limit is recorded, not enforced), handlers must be written to be idempotent, no dependency between jobs, one concurrent execution per schedule, assignment / workflow-task / evaluation reminders wait for their notification events (Phase 17 follow-up).
