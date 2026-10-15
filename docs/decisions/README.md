# Architecture Decision Records

Short, immutable records of significant decisions. Format: context → decision →
consequences. New records are appended; existing ones are superseded, not edited.

| ADR | Title | Status |
| --- | ----- | ------ |
| [0001](0001-modular-django-monolith.md) | Modular Django monolith | Accepted |
| [0002](0002-python-3-14-over-3-13.md) | Python 3.14 instead of 3.13 | Accepted |
| [0003](0003-tailwind-compiled-locally.md) | Tailwind CSS compiled locally (no CDN) | Accepted |
| [0004](0004-alpine-csp-build-self-hosted-js.md) | Alpine.js CSP build + self-hosted JS | Accepted |
| [0005](0005-native-django-tasks.md) | Native Django Tasks instead of Celery/Redis | Accepted |
| [0006](0006-custom-user-entra-ready.md) | Custom, Entra ID-ready user model | Accepted |
| [0007](0007-data-driven-roles.md) | Roles as data, not code | Accepted |
| [0008](0008-api-response-contract.md) | Versioned API with a response envelope | Accepted |
| [0009](0009-organization-identity.md) | Organization identity: UUID keys, immutable code, unique slug | Accepted |
| [0010](0010-generic-location-hierarchy.md) | Generic location hierarchy: one tree, configurable types, no fixed depth | Accepted |
| [0011](0011-organization-lifecycle-single-status.md) | One authoritative lifecycle field for organizations | Accepted |
| [0012](0012-policies-for-authorization.md) | Authorization decisions live in policies, shared by every channel | Accepted |
| [0013](0013-hierarchy-integrity-in-three-layers.md) | Hierarchy integrity enforced in three layers, including PostgreSQL | Accepted |
| [0014](0014-deactivate-not-delete.md) | Deactivate, never delete; no generic soft-delete column | Accepted |
| [0015](0015-tenant-isolation-through-selectors.md) | Tenant isolation enforced at one choke point: scoped selectors | Accepted |
| [0016](0016-platform-defined-location-types.md) | Location types may be platform-defined (nullable organization) | Accepted |
| [0017](0017-api-as-thin-adapter.md) | The REST API is a thin adapter over selectors, policies and services | Accepted |
| [0018](0018-safe-import-export.md) | Imports are all-or-nothing, code-keyed and tenant-safe | Accepted |
| [0019](0019-csp-compatible-frontend-conventions.md) | Frontend conventions that work under the strict CSP | Accepted |
| [0020](0020-admin-jazzmin-and-csp.md) | The admin: Jazzmin configured for AdminLTE 4, under a narrowly-widened CSP | Accepted |
| [0021](0021-css-variables-over-generated-css.md) | Runtime theming with CSS custom properties, not generated stylesheets | Accepted |
| [0022](0022-semantic-design-tokens.md) | Semantic design tokens as the only theming vocabulary | Accepted |
| [0023](0023-theme-ownership-and-lifecycle.md) | One Theme model, ownership by scope, a status-driven immutable lifecycle | Accepted |
| [0024](0024-theme-precedence-and-resolution.md) | Theme precedence: layered tokens, a separate colour mode, one resolver | Accepted |
| [0025](0025-controlled-token-validation.md) | Structured, validated token storage - not free-form JSON | Accepted |
| [0026](0026-no-arbitrary-custom-css-or-js.md) | No arbitrary custom CSS, HTML or JavaScript, ever | Accepted |
| [0027](0027-tailwind-integration-approach.md) | Tailwind integration: semantic utilities backed by theme variables | Accepted |
| [0028](0028-light-dark-system-appearance.md) | Light / dark / system appearance without a flash and without inline script | Accepted |
| [0029](0029-msal-for-entra-sign-in.md) | Microsoft Entra ID sign-in with MSAL, not a hand-rolled OAuth/OIDC client | Accepted |
| [0030](0030-user-lifecycle-status-authoritative.md) | User lifecycle: one authoritative status field, `is_active` as a mirror | Accepted |
| [0031](0031-lazy-validated-organization-context.md) | Organization context: lazily resolved, always validated, never trusted from the client | Accepted |
| [0032](0032-identity-provisioning-and-linking.md) | Identity provisioning and linking: default deny, approval before first sign-in | Accepted |
| [0033](0033-session-authenticated-api.md) | The REST API authenticates the session (no separate API tokens yet) | Accepted |
| [0034](0034-account-appearance-reuses-theme-engine.md) | The account appearance page reuses the theme engine, not a second preference store | Accepted |
