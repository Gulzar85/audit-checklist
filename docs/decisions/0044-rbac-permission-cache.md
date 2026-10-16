# 0044. RBAC grant cache

- **Status:** Accepted
- **Date:** 2026-10-16

## Decision
Resolved grants per (user, organization) and a user's visible organization ids are cached in Django's cache and
memoised on the request's user object. Keys carry a per-user and a global version; signals bump them on any change
to memberships, assignments, roles, role permissions, organizations, users and Django permission links, so an access
change takes effect on the next request. Location subtrees are **not** cached. `RBAC_CACHE_TTL` (default 300 s)
bounds staleness. Bulk `QuerySet.update()` skips signals and must call `invalidate_all()`.

## Consequences
Warm checks cost zero queries. The default cache is per-process: configure a shared backend before running several
workers.
