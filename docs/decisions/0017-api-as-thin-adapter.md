# 0017. The REST API is a thin adapter over selectors, policies and services

- **Status:** Accepted
- **Date:** 2026-10-15

## Context

The API must obey exactly the organization boundaries and hierarchy rules of the web
UI, and later support an offline-sync client.

## Decision

- ViewSets (`apps/api/v1/organizations.py`, `locations.py`) contain no business
  logic: `get_queryset()` is a selector; authorization is a *policy hook*
  (`policy_allows_collection` / `policy_allows_object`) consumed by
  `apps.api.permissions.PolicyPermission`; writes call a service
  (`perform_create/perform_update` and explicit actions).
- Serializers shape and validate input only. Fields the model *normalises*
  (`code`, `country`, `currency`...) skip DRF's raw-input validators so the service's
  `full_clean` is the single judge - `country: "gb"` works over the API as in the UI.
- Django `ValidationError` raised by services is converted by the central exception
  handler into the standard `{"error": {"code": "VALIDATION_ERROR", ...}}` envelope.
- **No DELETE.** Lifecycle changes are explicit actions
  (`POST .../status/`, `.../activate/`, `.../deactivate/`, `.../move/`), each with its
  own policy, log entry and history record. `status`/`is_active` are read-only fields.
- `GET /locations/tree/` returns the nested hierarchy (honouring the list's filters) in a
  constant number of queries; nesting is built iteratively, so depth is unbounded.
- Every resource exposes a stable `id` plus `created_at`/`updated_at`, which is what a
  later offline-sync engine needs (id + updated_at) without further API changes.

## Consequences

- Isolation, validation, audit and history cannot differ between channels.
- `tests/test_api_*.py` and `tests/test_tenant_isolation.py` cover permissions (401/403/404),
  filters, pagination, ordering, validation, invalid UUIDs and query counts.
