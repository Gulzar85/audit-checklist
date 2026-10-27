# 0083. Leases, conditional updates and idempotency

- **Status:** Accepted
- **Date:** 2026-10-27

## Decision
Claiming uses `SELECT ... FOR UPDATE SKIP LOCKED` and stamps a **lease** (random token + expiry). Starting and finishing a job are **conditional updates** on `(pk, status, lease_token)`; a runner that lost its lease (recovered, or the job was cancelled) matches no row and changes nothing. No transaction spans a handler. Expired leases are recovered at the start of every pass (a never-started job returns to the queue; a running one records a `lease_expired` attempt and retries or fails). Schedule occurrences are created inside the schedule's row lock under a unique `sched:<id>:<occurrence>` key; "run now" keys are caller-supplied (`Idempotency-Key`). Cancelling a *running* job is not supported: it finishes, which is safe because tasks must be idempotent and delegate to idempotent services (Phase 17 keys). Only sanitized text is stored; for unexpected errors only the exception class name.

## Consequences
Safe with several runners; a stuck runner delays one job until its lease expires (`JOBS_LEASE_SECONDS` must exceed the longest handler); a handler that overruns its lease may run twice.
