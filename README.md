# Checklist Platform

A generic, multi-organization **checklist & inspection platform** for enterprise use:
QA audits, operations inspections, restaurant and safety checks, and compliance
programmes all run on one configurable engine with scoring, findings, corrective
actions, workflow, and offline execution.

> **Phase 0 — Foundation.** This repository currently contains the architecture and
> platform foundation only. No business features are implemented yet (see
> [Scope](#phase-0-scope)).

---

## Tech stack

| Layer          | Choice                                                        |
| -------------- | ------------------------------------------------------------- |
| Runtime        | Python 3.14 (target 3.13), Django 6.1.2                       |
| Database       | PostgreSQL 17 (SQLite tolerated for isolated tests)          |
| API            | Django REST Framework + drf-spectacular (OpenAPI)            |
| Background     | Native Django Tasks framework (no Celery/Redis)              |
| Frontend       | Server-rendered Django templates + HTMX + Alpine.js + Lucide |
| Styling        | Tailwind CSS v4, compiled locally                            |
| PWA            | Web app manifest + service worker (offline shell)            |
| Identity       | Custom user model, Entra ID-ready (SSO in a later phase)     |
| Authz          | Data-driven RBAC + django-guardian (object permissions)      |

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

# 6. Run
.\venv\Scripts\python manage.py runserver
```

Then open <http://localhost:8000/> (landing page), `/admin/` (admin), and
`/api/docs/` (OpenAPI). Health probe: `/health/` and `/api/v1/health/`.

## Testing

```powershell
.\venv\Scripts\python -m pytest              # full suite
.\venv\Scripts\python -m pytest --cov=apps   # with coverage
```

Tests run against PostgreSQL using the `config.settings.test` module. Point
`TEST_DATABASE_URL` at a dedicated server if you prefer not to reuse
`DATABASE_URL`.

## Useful commands

```powershell
.\venv\Scripts\python manage.py check
.\venv\Scripts\python manage.py makemigrations
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
  core/            Base models, ActivityLog, health, PWA shell views, tasks
  organizations/   Tenant root (Organization) + memberships
  locations/       Location hierarchy and location types
  accounts/        Custom User (email login, Entra-ready) and auth views
  permissions/     RBAC foundation (Role / RoleScope / UserRole) + authorization
  notifications/   Provider-agnostic notification seam (SMTP now, M365 later)
  api/             DRF envelope renderer, exception handler, v1 + OpenAPI
  checklists/ executions/ findings/ corrective_actions/
  workflows/ scoring/ reporting/            (Phase 0 skeletons)
templates/         base shell, partials, pages, PWA, errors, tables2 template
static/            compiled css, vendored JS, generated icons
tailwind/          design-system source (input.css)
tests/             pytest suite
scripts/           one-off utilities (icon generation)
docs/              architecture notes and ADRs (docs/decisions)
```

---

## Configuration

All configuration is environment-driven via a dependency-free loader
(`config/settings/env.py`) that reads a local `.env` (never committed). See
`.env.example` for the full list. Highlights:

- `SECRET_KEY`, `DJANGO_DEBUG`, `DJANGO_ENVIRONMENT`, `DJANGO_ALLOWED_HOSTS`
- `DATABASE_URL` — `postgres://USER:PASSWORD@HOST:PORT/NAME` (percent-encode
  reserved characters in the password)
- `NOTIFICATION_EMAIL_PROVIDER` — swap the email provider without code changes
- `DJANGO_SECURE_*` — HSTS / SSL redirect in production

## Phase 0 scope

**Included (foundation):** project & settings structure, custom user, RBAC
foundation and authorization boundary, multi-organization model, locations,
response/error API contract, health checks, notifications seam, native Tasks
wiring, PWA shell, strict CSP, design system, admin, and a passing test suite.

**Deliberately excluded (later phases):** checklist authoring, executions,
scoring, findings, corrective actions, workflows, dashboards/reporting, the
offline execution engine and sync, Microsoft Entra ID login, Microsoft 365
email, AI features, Celery/Redis, microservices.

See [`docs/architecture.md`](docs/architecture.md) and
[`docs/decisions/`](docs/decisions/) for details.

## Notable decisions

- Python **3.14** (target was 3.13; 3.13 was unavailable in this environment).
- Tailwind CSS is **compiled locally** rather than loaded from a CDN.
- Alpine.js uses the **CSP build** so the Content-Security-Policy needs no
  `'unsafe-eval'`.
- All third-party JS is **self-hosted** (required by the strict CSP and by
  offline capability).

Each is captured as an ADR under [`docs/decisions/`](docs/decisions/).
