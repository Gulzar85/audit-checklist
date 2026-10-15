# Checklist Platform

A generic, multi-organization **checklist & inspection platform** for enterprise use:
QA audits, operations inspections, restaurant and safety checks, and compliance
programmes all run on one configurable engine with scoring, findings, corrective
actions, workflow, and offline execution.

> **Phase 2 — Identity, Profile & Organization Membership.** On top of the Phase 1 backbone, the
> platform now has its identity boundary: **Microsoft Entra ID sign-in** (MSAL), a single
> authoritative user lifecycle (`pending` / `active` / `suspended` / `inactive`), account and
> preference pages, organization switching, an identity API, and sign-in throttling. No
> checklist, scoring, finding or corrective-action functionality exists yet, and full
> authorization (roles/scopes) is a later phase (see [Scope](#scope)).

---

## Tech stack

| Layer          | Choice                                                        |
| -------------- | ------------------------------------------------------------- |
| Runtime        | Python 3.14 (target 3.13), Django 6.1.2                       |
| Database       | PostgreSQL 17 (required: recursive CTEs, triggers, row locks) |
| API            | Django REST Framework + drf-spectacular (OpenAPI)            |
| Background     | Native Django Tasks framework (no Celery/Redis)              |
| Frontend       | Server-rendered Django templates + HTMX + Alpine.js (CSP build) + Lucide |
| Styling        | Tailwind CSS v4, compiled locally                            |
| Forms / tables | crispy-forms + crispy-tailwind, django-filter, django-tables2 |
| History / I/O  | django-simple-history, django-import-export                  |
| PWA            | Web app manifest + service worker (offline shell)            |
| Identity       | Custom user model and Microsoft Entra ID sign-in (MSAL)     |
| Authz          | Policies over membership + model permissions (roles/scopes in Phase 4) |

---

## Prerequisites

- Python 3.13+ (3.14 verified)
- PostgreSQL 17 (running, with a role able to create databases)
- Node.js 20+ (only to build CSS)

## Quickstart

```powershell
# 1. Environment
python -m venv venv
.\venv\Scripts\pip install -r requirements\development.txt

# 2. Configuration
Copy-Item .env.example .env      # then edit SECRET_KEY and DATABASE_URL

# 3. Database
createdb checklist_platform

# 4. Migrate
.\venv\Scripts\python manage.py migrate

# 5. Frontend assets (Tailwind -> static/css/app.css)
npm install
npm run css:build

# 6. Optional: sample organizations and locations (development only)
.\venv\Scripts\python manage.py seed_demo_data
.\venv\Scripts\python manage.py createsuperuser

# 7. Run
.\venv\Scripts\python manage.py runserver
```

Then open <http://localhost:8000/> (landing page), `/organizations/`, `/locations/tree/`,
`/admin/` and `/api/docs/` (OpenAPI). Health probe: `/health/` and `/api/v1/health/`.

## Testing

```powershell
.\venv\Scripts\python -m pytest              # full suite (~1495 tests)
.\venv\Scripts\python -m pytest --cov=apps   # with coverage
```

Tests run against PostgreSQL using the `config.settings.test` module (the triggers,
recursive queries and row locks are part of what is under test). Point
`TEST_DATABASE_URL` at a dedicated server if you prefer not to reuse `DATABASE_URL`.

## Useful commands

```powershell
.\venv\Scripts\python manage.py check
.\venv\Scripts\python manage.py makemigrations --check
.\venv\Scripts\python manage.py validate_location_tree      # audit the hierarchy (non-zero exit on problems)
.\venv\Scripts\python manage.py seed_demo_data              # demo data; refuses in production
.\venv\Scripts\python manage.py spectacular --validate --fail-on-warn   # OpenAPI schema
.\venv\Scripts\python manage.py collectstatic --noinput
npm run css:watch            # rebuild CSS on change
npm run css:build            # minified production CSS
.\venv\Scripts\python scripts\generate_icons.py   # regenerate PWA icons
```

---

## Project layout

```
config/            Project configuration
  settings/        base / development / production / test + env loader
apps/
  core/            Base models, ActivityLog, mixins, shared forms/validators, health, PWA shell
  organizations/   Organization (tenant root), memberships, branding, services/policies/API serializers
  locations/       LocationType + Location tree: selectors (recursive CTEs), services,
                   policies, widgets, import/export, management commands
  accounts/        Custom User + lifecycle, Entra ID sign-in (MSAL), profile/preferences,
                   organization context, selectors/policies/services, auth middleware
  permissions/     RBAC foundation (Role / RoleScope / UserRole) + authorization
  notifications/   Provider-agnostic notification seam (SMTP now, M365 later)
  api/             DRF envelope renderer, exception handler, v1 (organizations, locations, identity) + OpenAPI
  checklists/ executions/ findings/ corrective_actions/
  workflows/ scoring/ reporting/            (Phase 0 skeletons - intentionally empty)
templates/         base shell, partials, components, tailwind/ crispy overrides, pages
static/            compiled css, vendored JS (+ components.js), generated icons
tailwind/          design-system source (input.css)
tests/             pytest suite
docs/              architecture notes (docs/architecture/) and ADRs (docs/decisions/)
```

Each domain app follows the same layering - **models** (persistence + invariants) →
**selectors** (scoped reads) → **policies** (may this user?) → **services** (writes: validated,
locked, logged) → thin **views / API viewsets / admin / imports** that only adapt.

---

## What Phase 1 provides

* **Organizations** - create / edit / view, branding (validated logo/favicon, brand colour),
  regional settings, a single authoritative lifecycle (`active` / `inactive` / `suspended`),
  immutable business `code`, unique `slug`, UUID public ids.
* **Location types** - each organization defines its own vocabulary (or uses platform-wide types).
* **Locations** - one generic tree, any depth: create, edit, move, deactivate (with a safe cascade),
  search, filter, paginate, searchable parent picker, expandable tree with HTMX search.
* **Integrity** - no self-parent, cycles, or cross-organization parent/type: enforced in the model,
  the services **and PostgreSQL** (constraints + triggers), including under concurrent edits.
* **Tenant isolation** - one scoping choke point shared by UI, API, admin and imports.
* **REST API** - `/api/v1/organizations/`, `/location-types/`, `/locations/`, `/locations/tree/`.
* **Admin** - an enterprise-grade Jazzmin console (branded, ordered, iconed, linked to the app; no third-party requests; runs under the strict CSP): scoped forms, status actions, members inline, import/export.
* **Audit** - `simple_history` (who/what/when per field) + `ActivityLog` business events.

Two very different hierarchies run on the same schema:

```
Org A:  Country → Region → Area → Branch → Location
Org B:  Business Unit → Division → Facility → Department
```

## What Phase 2 provides

* **Microsoft Entra ID sign-in** - MSAL authorization-code flow (state/nonce/PKCE and ID token
  validated by MSAL), tenant allow-listing, and a local password fallback that is disabled with
  `ENABLE_PASSWORD_LOGIN=False`. Tokens are never stored, logged or sent to the client.
* **One authoritative user lifecycle** - `pending` / `active` / `suspended` / `inactive`, with
  `is_active` as a database-enforced mirror, and safe messages for each refusal reason.
* **Provisioning & linking** - first sign-in creates a `pending` account for approval
  (`AUTH_PROVISIONING_MODE`); matching by Entra object id, then email under a policy that never
  attaches a privileged account (`AUTH_EMAIL_LINKING`).
* **Account & preferences** - profile view/edit (application-managed fields only), preferences
  and an appearance page that reuses the theme engine (ADR 0034).
* **Organization switching** - a validated, session-scoped active organization with a selector
  list; an organization the user cannot see is never adopted (ADR 0031).
* **Identity API** - `GET/PATCH /api/v1/me/` and `GET /api/v1/users/` (tenant-scoped, searchable).
* **Operational hardening** - sign-in throttling, mid-session lifecycle enforcement, safe
  `next` handling, and federated sign-out (`MICROSOFT_FEDERATED_LOGOUT`).

## Scope

**Phase 2 deliberately excludes** (later phases): checklist authoring, executions, responses,
scoring, findings, corrective actions, workflow screens, email notification workflows, offline
synchronisation, and the full role/scope authorization engine (Phase 4 - this phase establishes
the identity boundary, not the authorization model). Also excluded by design: Redis, Celery,
microservices, AI.

## Documentation

* [`docs/identity/`](docs/identity/) - sign-in (Entra/MSAL), user lifecycle, organization context, identity API
* [`docs/architecture/organizations-and-locations.md`](docs/architecture/organizations-and-locations.md) - domain model, hierarchy, indexes
* [`docs/architecture/security-and-permissions.md`](docs/architecture/security-and-permissions.md) - isolation, policies, uploads, audit
* [`docs/architecture/api.md`](docs/architecture/api.md) - endpoints, filters, errors
* [`docs/architecture/admin.md`](docs/architecture/admin.md) - the Jazzmin admin: branding, navigation, CSP posture, upgrades
* [`docs/architecture/operations.md`](docs/architecture/operations.md) - migrations, commands, demo data, verification, tests
* [`docs/architecture.md`](docs/architecture.md) - Phase 0 layering and conventions
* [`docs/decisions/`](docs/decisions/) - ADRs 0001-0034

---

## Configuration

All configuration is environment-driven via a dependency-free loader
(`config/settings/env.py`) that reads a local `.env` (never committed). See
`.env.example` for the full list. Highlights:

- `SECRET_KEY`, `DJANGO_DEBUG`, `DJANGO_ENVIRONMENT`, `DJANGO_ALLOWED_HOSTS`
- `DATABASE_URL` - `postgres://USER:PASSWORD@HOST:PORT/NAME` (percent-encode
  reserved characters in the password)
- `NOTIFICATION_EMAIL_PROVIDER` - swap the email provider without code changes
- `MICROSOFT_TENANT_ID` / `MICROSOFT_CLIENT_ID` / `MICROSOFT_CLIENT_SECRET` /
  `MICROSOFT_REDIRECT_URI` - Entra sign-in; when absent, only the local password fallback runs
- `AUTH_PROVISIONING_MODE`, `AUTH_EMAIL_LINKING`, `ENABLE_PASSWORD_LOGIN`,
  `MICROSOFT_FEDERATED_LOGOUT` - identity policy
- `AUTH_THROTTLE_MAX_ATTEMPTS` / `AUTH_THROTTLE_WINDOW_SECONDS` / `TRUSTED_PROXY_COUNT` - sign-in throttling
  (`TRUSTED_PROXY_COUNT` = reverse proxies you trust for `X-Forwarded-For`; default 0)
- `SESSION_ABSOLUTE_TIMEOUT_SECONDS` (8 h) / `SESSION_IDLE_TIMEOUT_SECONDS` (30 min, 0 = off) - session lifetime
- `ENABLE_PASSWORD_LOGIN` **must be `False` in production** - the production settings refuse to start otherwise
- `DJANGO_SECURE_*` - HSTS / SSL redirect in production

## Notable decisions

- Python **3.14** (target was 3.13; 3.13 was unavailable in this environment).
- Tailwind CSS is **compiled locally** rather than loaded from a CDN.
- Alpine.js uses the **CSP build**: all client state lives in named components
  (`static/js/components.js`); inline expressions and `x-model` do not work (ADR 0019).
- All third-party JS is **self-hosted** (required by the strict CSP and by offline capability).
- Organizations and locations are **deactivated, never deleted** (ADR 0014).
- Development runs the CSP in *report-only* mode (violations are logged, not blocked);
  production enforces it. Browsers note that report-only policies without a `report-uri`
  cannot report violations - that is expected in development. The application UI *and* the
  Jazzmin admin have been verified with the policy **enforced** and a clean console (ADR 0020).

Each is captured as an ADR under [`docs/decisions/`](docs/decisions/).

## UI design system

See [docs/ui-design-system.md](docs/ui-design-system.md): layouts, components, tokens, accessibility, and how to build a new page. Platform staff can browse every component at `/ui/`.
