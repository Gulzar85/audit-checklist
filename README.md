# Checklist Platform

A generic, multi-organization **checklist & inspection platform** for enterprise use:
QA audits, operations inspections, restaurant and safety checks, and compliance
programmes all run on one configurable engine with scoring, findings, corrective
actions, workflow, and offline execution.

> **Status: Phases 0-14 delivered.** Organizations and locations, identity (Microsoft Entra ID),
> RBAC, themes, checklist authoring/versioning/publishing with a conditional rules engine,
> assignments, execution, PWA offline execution and sync, evidence and attachments, findings,
> corrective actions, and a **configurable workflow engine** (Phase 14). Scoring, reporting and
> notification delivery are not built yet (see [Scope](#scope)).

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
| Authz          | RBAC: roles, scoped grants and policies over organization/location scope |

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
.\venv\Scripts\python -m pytest              # full suite (~3,900 tests; the full run takes about 45 minutes)
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
.\venv\Scripts\python manage.py seed_demo_checklists --organization <slug>   # checklist demos (incl. conditional rules)
.\venv\Scripts\python manage.py seed_demo_versioning --organization <slug>   # versioning walkthrough (v1 -> v2, comparison)
.\venv\Scripts\python manage.py seed_demo_assignments --organization <slug>  # assignments: hierarchy, v1 checklist, draft/issued/cancelled
.\venv\Scripts\python manage.py seed_demo_executions --organization <slug>   # executions: v1 checklist, in-progress and submitted runs
.\venv\Scripts\python manage.py seed_demo_offline --organization <slug>   # a fresh assignment for the offline walkthrough (docs/offline-execution.md)
.\venv\Scripts\python manage.py seed_demo_evidence --organization <slug>   # a checklist with photo/file/signature questions and a fresh assignment
.\venv\Scripts\python manage.py seed_demo_findings --organization <slug>   # a rule-driven inspection, findings in several states (docs/findings-operations.md)
.\venv\Scripts\python manage.py seed_demo_corrective_actions --organization <slug>   # actions in several states against a demo finding (docs/corrective-actions-architecture.md)
.\venv\Scripts\python manage.py seed_workflows --organization <slug>   # corrective action verification + critical finding review workflows (docs/workflow-administration.md)
.\venv\Scripts\python manage.py release_workflow_orphans          # release workflow tasks of inactive assignees
.\venv\Scripts\python manage.py detect_findings --dry-run                  # idempotently (re)raise findings of submitted executions
.\venv\Scripts\python manage.py prune_sync_receipts --days 90   # delete old offline idempotency receipts
.\venv\Scripts\python manage.py scan_evidence                   # scan files awaiting the malware check (docs/evidence-malware-scanning.md)
.\venv\Scripts\python manage.py reconcile_evidence --dry-run    # find orphaned/missing evidence files (docs/evidence-operations.md)
node --test static/js/offline/tests/   # JavaScript tests of the offline engine (also run by pytest)
.\venv\Scripts\python manage.py seal_checklist_versions   # record integrity hashes of versions published before Phase 7
.\venv\Scripts\python manage.py verify_checklist_integrity   # non-zero exit if a published definition was altered
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
  themes/          Theme engine (design tokens, appearance)
  checklists/      Authoring, versions, publishing, answer types, Phase 6 rules engine
  assignments/     Issuing checklists to people/locations, bulk assignment, lifecycle
  executions/      Running a checklist: responses, validation, submission
  evidence/        Files, photos, signatures: upload pipeline, scanning, attachment targets
  findings/        Findings engine: detection from submitted executions, lifecycle
  corrective_actions/  Corrective actions raised from findings: lifecycle, verification
  workflows/       Configurable workflow engine: definitions, versions, tasks, approvals
  scoring/ reporting/                       (skeletons - intentionally empty)
templates/         base shell, partials, components, tailwind/ crispy overrides, pages
static/            compiled css, vendored JS (+ components.js), generated icons
tailwind/          design-system source (input.css)
tests/             pytest suite
docs/              architecture notes (docs/architecture/) and ADRs (docs/decisions/)
```

Each domain app follows the same layering - **models** (persistence + invariants) ->
**selectors** (scoped reads) -> **policies** (may this user?) -> **services** (writes: validated,
locked, logged) -> thin **views / API viewsets / admin / imports** that only adapt.

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
Org A:  Country -> Region -> Area -> Branch -> Location
Org B:  Business Unit -> Division -> Facility -> Department
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

## What Phases 3-14 add

* **Phase 3-4 - Themes and RBAC:** a theme engine, and roles with organization/location scope; every read and write is scoped ([docs/rbac.md](docs/rbac.md)).
* **Phase 5-8 - Checklists:** authoring, answer types, the Phase 6 conditional rules engine (three-valued logic), immutable published versions with integrity hashes, and assignments with bulk issue.
* **Phase 9 - Execution:** start/resume against the exact published version, server-side validation and rule evaluation, optimistic concurrency, transactional submission.
* **Phase 10 - PWA offline:** IndexedDB execution, a versioned sync protocol with idempotency receipts and conflict resolution.
* **Phase 11 - Evidence:** upload pipeline, storage, malware-scan seam, attachment targets, offline evidence sync.
* **Phase 12 - Findings:** rule-driven detection from submitted executions, severities and categories, lifecycle.
* **Phase 13 - Corrective actions:** owners, due dates, completion evidence, independent verification.
* **Phase 14 - Workflow engine:** versioned definitions (draft -> published, sealed by DB triggers), states/transitions with Phase 6 conditions, assignee policies, approvals (one/all/min), separation of duties, deadlines, a registry of business actions that call the Findings and Corrective Action services, designer and runtime UI, REST API and read-only admin. Start with `seed_workflows` ([docs/workflow-administration.md](docs/workflow-administration.md)).

The REST API root `/api/v1/` lists every endpoint; the OpenAPI schema is at `/api/docs/`.

## Scope

**Not built yet:** scoring, reporting/dashboards, notification delivery (email/Teams), schedulers and recurring jobs, reopening submitted executions. Also excluded by design: Redis, Celery, microservices, a SPA, a second rules engine, arbitrary user code in workflows.

## Documentation

* [`docs/identity/`](docs/identity/) - sign-in (Entra/MSAL), user lifecycle, organization context, identity API
* [`docs/architecture/`](docs/architecture/) - organizations and locations, security, API, admin, operations; [`docs/architecture.md`](docs/architecture.md) - layering and conventions
* [`docs/rbac.md`](docs/rbac.md), [`docs/authorization.md`](docs/authorization.md), [`docs/access-control-matrix.md`](docs/access-control-matrix.md) - authorization
* Checklists: [`docs/checklists.md`](docs/checklists.md), [`docs/conditional-rules.md`](docs/conditional-rules.md), [`docs/checklist-versioning.md`](docs/checklist-versioning.md), [`docs/answer-types.md`](docs/answer-types.md)
* Assignments and execution: [`docs/checklist-assignment-architecture.md`](docs/checklist-assignment-architecture.md), [`docs/checklist-execution-architecture.md`](docs/checklist-execution-architecture.md)
* Offline: [`docs/offline-execution.md`](docs/offline-execution.md), [`docs/offline-sync-protocol.md`](docs/offline-sync-protocol.md), [`docs/pwa-architecture.md`](docs/pwa-architecture.md)
* Evidence: [`docs/evidence-architecture.md`](docs/evidence-architecture.md), [`docs/evidence-operations.md`](docs/evidence-operations.md)
* Findings: [`docs/findings-architecture.md`](docs/findings-architecture.md), [`docs/findings-operations.md`](docs/findings-operations.md)
* Corrective actions: [`docs/corrective-actions-architecture.md`](docs/corrective-actions-architecture.md), [`docs/corrective-action-lifecycle.md`](docs/corrective-action-lifecycle.md)
* Workflows: [`docs/workflow-architecture.md`](docs/workflow-architecture.md), [`docs/workflow-runtime.md`](docs/workflow-runtime.md), [`docs/workflow-versioning-and-validation.md`](docs/workflow-versioning-and-validation.md), [`docs/workflow-administration.md`](docs/workflow-administration.md)
* API: [`docs/api/`](docs/api/) (one file per module, including [workflows](docs/api/workflows.md))
* [`docs/decisions/`](docs/decisions/) - ADRs 0001-0072

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

