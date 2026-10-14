# 0007. Roles as data, not code

- **Status:** Accepted
- **Date:** 2026-10-14

## Context

Every organization structures responsibility differently. Hard-coding role
names in business logic would couple the platform to one org chart.

## Decision

Model roles as **data**: `Role` (optionally organization-scoped), `RoleScope`
(platform / organization / location), and `UserRole` (assignment). Access
decisions go through the `apps.permissions.authorization` boundary; business
code never branches on role names.

## Consequences

- Tenants define their own roles without code changes.
- Authorization entrypoints stay stable while policy evolves behind them.
- Django's built-in permissions/groups and django-guardian remain available for
  fine-grained and object-level checks.
