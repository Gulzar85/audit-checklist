# 0006. Custom, Entra ID-ready user model

- **Status:** Accepted
- **Date:** 2026-10-14

## Context

Microsoft Entra ID will be the primary identity provider. Swapping the user
model later is painful, so the right model must exist from the start.

## Decision

Use a **custom user model** (`accounts.User`) from the first migration:
UUID primary key, **email** as the login field, optional `entra_object_id`
(unique), and local passwords as an administrative fallback only.

## Consequences

- Entra ID can be added without a disruptive user-model migration.
- Email is the natural key; `get_by_natural_key` is case-insensitive.
- Local passwords exist for bootstrap/admin access but are not the strategy.
