# Scheduling: runner, deployment and operations

## Local development
```bash
.\venv\Scripts\python manage.py migrate
.\venv\Scripts\python manage.py seed_permissions
.\venv\Scripts\python manage.py seed_roles
.\venv\Scripts\python manage.py seed_default_schedules      # platform-wide maintenance schedules (idempotent)
.\venv\Scripts\python manage.py process_scheduled_jobs      # one bounded pass
.\venv\Scripts\python manage.py process_scheduled_jobs --loop --poll-interval 15   # keep polling; Ctrl+C stops cleanly
.\venv\Scripts\python manage.py inspect_scheduled_jobs      # health (exit 1 = needs attention)
```
Open `/scheduling/` (administrators; `Diagnostics` for superusers). Without a runner nothing executes: `Run now` only queues a job.

## Commands
| Command | Purpose |
|---|---|
| `process_scheduled_jobs [--limit N] [--max-seconds S] [--loop] [--poll-interval S] [--runner-id NAME] [--dry-run]` | recover expired leases, turn due schedule occurrences into jobs, run due jobs; bounded (`limit` 1-200, `max-seconds` <= 3300); `--dry-run` reports only. Exit 0 = pass completed (individual job failures are recorded on the jobs); **2** = the runner itself failed. |
| `run_scheduled_job JOB_ID` | claim and run one due job (operators) |
| `retry_failed_jobs [--task CODE] [--limit N] [--reason TEXT]` | re-queue failed jobs (audited) |
| `inspect_scheduled_jobs` | runner heartbeat, backlog, expired leases, repeated failures; exit 1 when something needs attention |
| `seed_default_schedules` | create platform-wide defaults: notifications every 5 minutes, corrective-action reminders daily 07:00 UTC, notification purge 02:30, scheduling cleanup 03:00 (never overwrites an existing schedule) |
Overlapping invocations are safe (row locks and leases). On SIGINT/SIGTERM the runner stops starting jobs and releases the ones it claimed but did not start.

## Production: Windows Task Scheduler (this deployment)
1. Create an `Environment`/service account with access to the project and the production `.env`.
2. Task: **Trigger** daily, repeat every **1 minute** indefinitely. **Action** `Start a program`: Program `D:\Audit\venv\Scripts\python.exe`, Arguments `manage.py process_scheduled_jobs --limit 50 --max-seconds 50`, Start in `D:\Audit`. Settings: *Do not start a new instance if already running* is **not required** (overlap is safe) but keeps the process count low; *Stop the task if it runs longer than* 10 minutes.
3. Or one task at start-up running `process_scheduled_jobs --loop --poll-interval 30` (restart on failure).
4. Verify with `manage.py inspect_scheduled_jobs` (runner `alive`).
PowerShell example:
```powershell
$action  = New-ScheduledTaskAction -Execute "D:\Audit\venv\Scripts\python.exe" -Argument "manage.py process_scheduled_jobs --limit 50 --max-seconds 50" -WorkingDirectory "D:\Audit"
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date) -RepetitionInterval (New-TimeSpan -Minutes 1)
Register-ScheduledTask -TaskName "Checklist scheduler" -Action $action -Trigger $trigger
```
## Linux
cron: `* * * * * cd /srv/audit && ./venv/bin/python manage.py process_scheduled_jobs --limit 50 --max-seconds 50`. systemd: a `oneshot` service running the command plus a timer with `OnCalendar=*:0/1`, or a `Type=simple` service with `--loop` and `Restart=always`.

## Trade-offs
Polling gives latency up to the interval (one minute with the 1-minute trigger) and an empty pass costs a few indexed queries. A resident `--loop` runner reduces latency and process starts but needs a supervisor. If the host sleeps or the task is disabled, nothing runs: missed schedule runs follow the schedule's misfire policy and open jobs wait. Execution is at-least-once, never exactly-once.

## Configuration (environment)
`JOBS_EXECUTION_BACKEND` (`poll` default | `django_tasks`, only with a real Django Tasks worker), `JOBS_LEASE_SECONDS` (600; must exceed the longest handler), `JOBS_MISFIRE_GRACE_SECONDS` (300), `JOBS_RETRY_JITTER` (0.1), `JOBS_HISTORY_RETENTION_DAYS` (90), `JOBS_RUNNER_ID` (default `host:pid`). The notification settings are unchanged (`NOTIFICATION_*`).

## Monitoring and troubleshooting
* `/scheduling/diagnostics/` (superusers) and `inspect_scheduled_jobs`: **runner never seen / silent / active**, ready and overdue jobs, expired leases, retries, schedules past their time, repeated failures. A silent runner with an empty queue is *not* the same as an idle healthy runner: the screen says which.
* Jobs stay pending: no runner (check the Task Scheduler task and its last result) or `available_at` in the future (retry backoff / delay).
* A job is `claimed`/`running` for long: the next pass recovers it after the lease expires.
* Repeated failures: open the job; the attempts list shows category and decision. Fix the cause, then **Retry** (reason required) or `retry_failed_jobs`.
* A schedule that never runs: it is paused/disabled/finished, or its end passed; resuming never back-fills.

## Migrations and tests
`manage.py migrate` applies `apps/jobs/migrations/0001_initial.py` (four new tables, no change to existing ones; it can be reversed by unapplying `jobs`). Migrations are git-ignored in this repository by project convention; generate with `makemigrations jobs`.
```bash
.\venv\Scripts\python -m pytest tests/test_jobs_core.py tests/test_jobs_surfaces.py
```
