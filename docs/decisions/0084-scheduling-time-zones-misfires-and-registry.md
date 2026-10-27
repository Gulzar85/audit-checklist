# 0084. Time zones, misfires and the task registry

- **Status:** Accepted
- **Date:** 2026-10-27

## Decision
* Timestamps are UTC; the schedule's IANA zone is stored. Wall-clock recurrences follow the local clock; a non-existent local time runs shifted forward, an ambiguous one once at its first occurrence. Changing a zone needs explicit confirmation and recomputes from *now*; edit/enable/resume never back-fill.
* Misfires are explicit per schedule: **skip** (record one skipped job, jump to the next future run) or **run once** (one catch-up job). A schedule never overlaps itself (the occurrence becomes a recorded skip).
* The registry is the only way to run anything: stable codes, strict schemas, scope, permission, retry policy, a handler calling an existing service. Platform-wide tasks and schedules are superuser-only; organization tasks need `jobs.manage` / `jobs.run` in that organization. Assignment and workflow-task reminders are **not registered** because the notification registry has no such events; they follow once Phase 17 adds them.

## Consequences
Predictable behaviour across daylight saving and downtime; tenants cannot reach platform-wide work; no arbitrary code, SQL or shell is ever scheduled.
