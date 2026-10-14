# 0005. Native Django Tasks instead of Celery/Redis

- **Status:** Accepted
- **Date:** 2026-10-14

## Context

Background work (reminders, notifications, reporting) is needed eventually, but
Phase 0 must avoid operational complexity and extra infrastructure.

## Decision

Use the **native Django Tasks framework** (`django.tasks`) with
`ImmediateBackend` in development and tests. Task definitions live in
`apps/*/tasks/`. A production backend with a real worker is introduced later.

## Consequences

- No Celery/Redis dependency; tasks are defined and invoked with Django.
- Code never depends on the backend; swapping it is configuration only.
- Long-running workloads are deferred until a real worker backend is chosen.
