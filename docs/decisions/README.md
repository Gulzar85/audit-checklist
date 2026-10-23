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
| [0035](0035-membership-status-lifecycle.md) | Membership lifecycle: one authoritative status, rows are kept | Accepted |
| [0036](0036-location-scope-foundation.md) | Location scope: a data foundation without an engine | Accepted |
| [0037](0037-user-preferences-allow-list.md) | User preferences: an allow-listed key/value store; appearance stays in the theme engine | Accepted |
| [0038](0038-session-lifetime-and-pwa.md) | Session lifetime, expiry responses and PWA behaviour | Accepted |
| [0039](0039-application-shell-and-navigation.md) | The application shell: sidebar layout with configuration-driven navigation | Accepted |
| [0040](0040-component-architecture-and-ui-rules.md) | Component architecture: partials + template tags, state in Alpine, rules enforced by tests | Accepted |
| [0041](0041-status-vocabulary-and-responsive-tables.md) | One status vocabulary and responsive tables | Accepted |
| [0042](0042-layered-authorization-model.md) | Layered authorization: membership, role, scope | Accepted |
| [0043](0043-policies-escalation-and-404-403.md) | Policies, no escalation, and the 404 / 403 rule | Accepted |
| [0044](0044-rbac-permission-cache.md) | RBAC grant cache | Accepted |
| [0045](0045-generic-checklist-definition-model.md) | A generic, versioned checklist definition model | Accepted |
| [0046](0046-answer-type-engine.md) | The answer-type engine: a code registry with schema-validated configuration | Accepted |
| [0047](0047-conditional-rules-engine.md) | Conditional rules: declarative, versioned, validated as a set, evaluated by one pure function | Accepted |
| [0048](0048-version-lifecycle-and-archival.md) | Version lifecycle: draft, published, superseded; archival belongs to the template | Accepted |
| [0049](0049-published-definitions-are-immutable.md) | Published definitions are immutable: services, models and PostgreSQL triggers | Accepted |
| [0050](0050-version-manifest-and-integrity-hash.md) | A derived manifest and SHA-256 integrity hash (not a snapshot) | Accepted |
| [0051](0051-checklist-assignment-model-and-lifecycle.md) | Checklist assignments: exact version, explicit lifecycle, derived overdue | Accepted |
| [0052](0052-duplicate-policy-and-bulk-all-or-nothing.md) | One open assignment per version and location; bulk assignment is all-or-nothing | Accepted |
| [0053](0053-assignment-authorization-and-scope.md) | Assignment authorization: scope in the selector, 404 for invisible, 403 for forbidden | Accepted |
| [0054](0054-executions-and-responses.md) | Executions and responses: one live execution per assignment, relational answers, frozen once submitted | Accepted |
| [0055](0055-save-concurrency-and-submission.md) | Saving and submission: revisions, locks and one transaction | Accepted |
| [0056](0056-evidence-boundary-and-hidden-answers.md) | Evidence boundary and hidden-answer retention | Accepted |
| [0057](0057-offline-sync-protocol-and-idempotency.md) | Offline synchronization: per-operation atomic, ordered, idempotent by durable receipts | Accepted |
| [0058](0058-offline-client-evaluator-and-compatibility-policy.md) | A small, versioned client evaluator and a refuse-don't-degrade compatibility policy | Accepted |
| [0059](0059-offline-storage-service-worker-and-privacy.md) | Offline storage, service-worker allowlist and privacy | Accepted |
| [0060](0060-evidence-model-and-target-registry.md) | Evidence: asset/attachment model, explicit targets, immutable facts | Accepted |
| [0061](0061-upload-pipeline-lifecycle-and-scanning.md) | Upload pipeline, lifecycle and scanning | Accepted |
| [0062](0062-offline-evidence-over-the-existing-sync.md) | Offline evidence over the existing sync engine | Accepted |
| [0063](0063-finding-model-lifecycle-and-source-registry.md) | Findings: explicit-key model, controlled sources, resolved is not verified | Accepted |
| [0064](0064-finding-detection-on-submission-and-idempotency.md) | Finding detection: a Phase 6 action, raised on submission, idempotent | Accepted |
| [0065](0065-finding-evidence-read-through.md) | Finding evidence is read through the triggering response | Accepted |
| [0066](0066-corrective-action-model-and-lifecycle.md) | Corrective actions: relational model, append-only history, derived overdue | Accepted |
| [0067](0067-verification-and-separation-of-duties.md) | Verification and separation of duties | Accepted |
| [0068](0068-finding-eligibility-hooks-and-evidence-target.md) | Parent-finding eligibility through hooks; the corrective-action evidence target | Accepted |
| [0069](0069-workflow-model-states-and-versions.md) | Workflow model: states carry the approval stage, versions are sealed, instances bind a version | Accepted |
| [0070](0070-workflow-validation-and-phase6-conditions.md) | Publication validation and Phase 6 conditions | Accepted |
| [0071](0071-target-and-action-registries-and-governance.md) | Target and business-action registries, transactions and domain governance | Accepted |
| [0072](0072-tasks-assignment-approvals-and-concurrency.md) | Tasks, assignment, approvals, separation of duties and concurrency | Accepted |
