# Scheduling API (`/api/v1/scheduling/`)

Standard envelope; lists carry `meta.count` (`?limit=` max 100, `?offset=`). Unknown fields are rejected. Another organization's schedule or job is a 404. Statuses are never writable; payloads, tokens and exception text are never returned. Consequential calls are covered by the per-user API throttle; run-now accepts an `Idempotency-Key` header (a repeat returns the first job).

| Method and path | Purpose | Needs |
|---|---|---|
| `GET tasks/` | the registry and what the caller may schedule / trigger | sign-in |
| `GET schedules/` (`status`, `task_type`, `q`) | schedules | `jobs.view` |
| `POST schedules/` | create (`platform_wide` for superusers) | `jobs.manage` (superuser for platform-wide tasks) |
| `GET/PUT schedules/{id}/` | read / edit (`allow_timezone_change` confirms a zone change) | view / manage |
| `POST schedules/{id}/enable|disable|pause|resume/` | state change (409 when invalid) | manage |
| `POST schedules/{id}/run/` | queue one extra run (202) | task permission (`jobs.run`) |
| `GET upcoming/` | next runs of active schedules | view |
| `GET jobs/` (`status`, `task_type`, `schedule`) | job history | view |
| `POST jobs/` | run a registered task now or after `delay_seconds` (202) | task permission |
| `GET jobs/{id}/` | job and its attempts | view |
| `POST jobs/{id}/cancel|retry/` | `{"reason": "..."}`; cancel only before it starts, retry only a failed job (409 otherwise) | `jobs.run` |
| `GET diagnostics/` | runner health | superuser |
