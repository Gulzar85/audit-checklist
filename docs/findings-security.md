# Findings: organization isolation, RBAC and audit

## Permissions (`apps/permissions/registry.py`)
`finding.view`, `finding.create`, `finding.update`, `finding.assign`, `finding.acknowledge`, `finding.resolve`, `finding.close`,
`finding.reopen`, `finding.cancel` are **location-scoped** (a grant at a location covers it and its subtree). `finding.manage`
(categories, severity levels) is organization-wide. History is visible to anyone who can view the finding (there is no separate
`view_history`). Existing roles were extended: Administrator and QA Manager hold all; Operations Manager acknowledge/assign/resolve;
Restaurant Manager acknowledge/resolve; Auditor view/create. After deployment run `manage.py seed_permissions` and `seed_roles`.

## Visibility
`selectors.findings_visible_to(user)` is the only way to list findings: organizations the user may enter x locations where they hold
`finding.view` (subtrees expanded), plus findings assigned to them. Filters, search, ordering and pagination are applied on top, and
filter choices are drawn from the same scope. An invisible, foreign or malformed id is **404** everywhere (list, detail, actions,
evidence); a visible finding the user may not act on is **403**; a repeat or race is **409**.

## What a client cannot do
Supply or change the organization, creator (`detected_by`), source, detection data, number, status, resolution stamps or traceability
(unknown fields are rejected with 400, not ignored); choose a category/severity/location/execution from another organization or
outside their scope (validation error "unknown ..."); link a question that is not in the execution's version; write `status` through
PATCH, forms, admin or import; trigger rule evaluation (there is no such endpoint - findings are raised by the server on submission).
These are enforced in the services, again by database triggers, and by constraints.

## Evidence
Evidence is reached only through its finding: the attachment must be one of that finding's own (never another record's), the user
must be able to see the finding, and the asset must be available (never quarantined/unscanned). Phase 11 download headers apply
(`nosniff`, `no-store`, sandbox CSP, attachment unless an image), and every serve is audited.

## Admin
Read-only for findings, categories and severities (no add/change/delete, no bulk actions); non-superusers see only their scope.
Configuration changes go through `finding.manage` services so unique rank, one default and immutable codes always apply.

## Audit
`finding.created`, `finding.raised` (rule, source, severity, category, review flag), `finding.detection_run`, `finding.detection_failed`,
`finding.updated` (before/after), `finding.severity_changed`, `finding.category_changed`, `finding.due_date_changed`,
`finding.assigned`, `finding.acknowledged|started|resolved|closed|reopened|cancelled` (reason, previous status),
`finding.transition_refused`, `finding_category.saved`, `finding_severity.saved`; every entry carries `finding_id` for the timeline.
No evidence contents are logged. History rows (`simple_history`) are written for every model change.
