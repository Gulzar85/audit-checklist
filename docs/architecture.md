# Architecture — Phase 0 foundation

This document describes the layering, boundaries and conventions established in
Phase 0. Later phases add business modules without changing these foundations.

> **Phase 1** (organizations and the location hierarchy) builds on this and adds
> `policies.py` per domain, `PolicyMixin`, scoped selectors and a PostgreSQL integrity
> layer. See [`architecture/`](architecture/) and ADRs 0009-0019.

## Guiding principles

1. **Modular monolith.** One deployable Django project, split into apps with
   clear responsibilities. No microservices.
2. **Single responsibility per app.** Each app owns its models, services,
   selectors (read helpers), views and admin.
3. **Thin views, explicit services.** Authorization and business decisions live
   behind named boundaries, never inline in views or templates.
4. **Roles are data, not code.** No role name is hard-coded in business logic.
5. **Class-based views.** Every application page is a CBV. Only framework
   callbacks (e.g. error handlers) may be function-based.
6. **Configuration over hard-coding.** Environment-driven settings; nothing
   sensitive in the repository.

## Layering

```
        ┌──────────────────────────────────────────────────┐
HTTP →  │ views / API views (CBV, APIView)                  │
        ├──────────────────────────────────────────────────┤
        │ authorization boundary  (apps.permissions)        │
        │ services / notifications (write, orchestrate)      │
        ├──────────────────────────────────────────────────┤
        │ selectors (read helpers)  ·  models (persistence)  │
        └──────────────────────────────────────────────────┘
                     PostgreSQL
```

- **Models** describe persistence and domain invariants only.
- **Selectors** are read-only query helpers (e.g. `organizations_visible_to`,
  `get_descendants`); tenant-owned selectors all build on the visibility selector.
- **Policies** (`policies.py`) answer "may this user do this?" for every channel.
- **Services** perform writes/orchestration (validated, locked, logged).
- **Authorization** is asked via `apps.permissions.authorization`; views and
  templates must not inspect roles directly.

## Request lifecycle

1. `SecurityMiddleware` and `ContentSecurityPolicyMiddleware` add security
   headers (CSP carries a per-request nonce).
2. Session/auth middleware populate the request; `simple_history` records the
   acting user.
3. The view delegates to selectors/services and renders a template (HTML) or a
   DRF `Response` (API), which the envelope renderer wraps as
   `{"data": ..., "meta": ...}`.
4. Exceptions in the API are converted by `api_exception_handler` into
   `{"error": {"code", "message", "details"}}`.

## App responsibilities

| App                  | Responsibility                                                    |
| -------------------- | ----------------------------------------------------------------- |
| `core`               | Abstract base models, `ActivityLog`, health, PWA/shell views, tasks |
| `organizations`      | Tenant root (lifecycle, branding, settings) and user↔organization membership |
| `locations`          | Self-referencing location tree and location types, hierarchy selectors/services |
| `accounts`           | Custom `User` (email login, `entra_object_id`), auth views, admin  |
| `permissions`        | `Role` / `RoleScope` / `UserRole`, selectors, authorization        |
| `notifications`      | Provider-agnostic notification service + providers                |
| `api`                | Response contract, renderer, exceptions, pagination, v1, OpenAPI  |
| `checklists` …       | Empty skeletons reserved for later phases                         |

## Authorization boundary

`apps.permissions.authorization` is the **only** sanctioned way to ask "may this
user do this?". Phase 0 implements a conservative baseline (an active,
authenticated user) behind stable signatures:

```python
can_view_checklist(user, checklist)
can_execute_checklist(user, execution)
can_manage_finding(user, finding)
can_verify_action(user, corrective_action)
has_role(user, role_slug, *, organization=None)
```

Object-level policy is added inside these functions in later phases without
changing call sites.

## Notifications seam

Business code never talks to SMTP directly. It calls
`apps.notifications.services.notification_service`, which resolves a provider
from `NOTIFICATION_EMAIL_PROVIDER` (default `DjangoEmailProvider`; a Microsoft
365 provider is stubbed). Swapping providers is a settings change, not a code
change.

## Background tasks

The native Django Tasks framework is wired with `ImmediateBackend` for
development/tests. Task definitions live in `apps/*/tasks/`. Production swaps in
a backend with a real worker; call sites are unaffected.

## API contract

- Success: `{"data": <payload>, "meta": {...}}`
- Error: `{"error": {"code", "message", "details"}}`
- Versioning: `NamespaceVersioning`, default and only version `v1`
- Schema/UI: `/api/schema/` and `/api/docs/`

## Security & CSP

- A single strict CSP (`BASE_CSP`) is shared; dev runs **report-only**,
  production **enforces** it.
- Scripts/styles are covered by a per-request **nonce** (`{% csp_nonce_attr %}`),
  not `'unsafe-inline'`. Alpine.js uses its **CSP build** to avoid
  `'unsafe-eval'`.
- Third-party JS is self-hosted under `static/vendor/` (CSP + offline).

## PWA & offline

The shell links a root-scoped manifest and registers a service worker
(`/service-worker.js`). The worker precaches an offline shell and static assets.
The offline execution engine (IndexedDB persistence + background sync queue) is
reserved as a documented extension point in `static/js/service-worker.js`.
