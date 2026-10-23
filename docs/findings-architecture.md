# Findings architecture (Phase 12)

A **finding** is a traceable business record - an issue, nonconformity or exception - not merely a failed checklist answer. It has an
owner, a severity, a lifecycle, evidence and a history, and it is independent of any industry: categories and severity levels are
organization data.

## Domain model (`apps/findings`)

| Model | Purpose | Key integrity |
|---|---|---|
| `FindingCategory` | organization-owned classification (`safety`, `quality` ... are only the seeded starting set) | unique `(organization, code)`, code pattern check, never deleted (deactivate), `PROTECT` from findings |
| `FindingSeverity` | ordered levels: `rank` (1-100, higher = more severe), `requires_owner`, `default_due_days`, `is_escalation`, `is_default` | unique `(organization, rank)`, one default per organization, rank range check |
| `Finding` | the record | see below |

Severity is **always compared by `rank`**, never by label or alphabetically; an organization may rename, add or reorder levels.
`requires_owner` and `default_due_days` are policy applied by the services; `is_escalation` is metadata for the future
escalation/notification phase (counted as *critical* on the dashboard; nothing is sent in Phase 12).

### `Finding`
* identity: UUID, organization, `number` (per organization, `FND-00042`, allocated under the organization row lock);
* content: `title`, `description`, `category`, `severity`, `needs_review`, `inherit_evidence`;
* ownership/dates: `assigned_to`, `due_at` (never invented: only set by the user or by the severity's `default_due_days`), `detected_at`, `detected_by`;
* lifecycle: `status`, `resolution_summary`, `resolved_*`, `closed_*`, `cancelled_*`, `cancel_reason`, `reopen_count`;
* **source traceability** (checklist findings): `assignment`, `execution`, `version` (the exact checklist version), `item`, `response`, `rule_code`, `rule_name`, `detection_key`.

All relationships are explicit foreign keys with `PROTECT`; there is **no GenericForeignKey** and no client-controlled model name.
The source is one of the controlled `SourceType` values (`checklist_rule`, `manual`); a future source (corrective-action
verification, integrations) is added by extending that registry plus its own validating service - never by accepting a type from a client.

### Historical behaviour
A finding stores the version, item and response that raised it. Publishing a new checklist version, renaming a category or
changing a severity rank never alters what a finding refers to. Findings are **never deleted** (a database trigger refuses `DELETE`);
a user or template going inactive leaves the record intact (`SET_NULL` on people, `PROTECT` on everything else). Every change is
in `django-simple-history` and the activity log.

## Database guarantees (`guards.py`, constraints)
* organization consistency of finding / category / severity / location / assignment / execution (trigger);
* the execution runs the finding's assignment and version; the location is the assignment's; the item belongs to that version; the
  response belongs to that execution and item (trigger);
* number, organization, source, location, `detection_key`, `rule_code`, `detected_at` and every traceability key are immutable (trigger);
* `UNIQUE(organization, detection_key)` where set - one finding per trigger, under any concurrency;
* a rule-sourced finding must carry its whole source (check); resolved/closed need their timestamps and cancelled needs a reason (checks).

## Layering
`models` -> `services/` (`detection`, `lifecycle`, `config`; the only writers) -> `selectors` (authorized reads) -> `policies`
(authorization) -> `views`/`forms`/`filters`/`tables` (web), `apps/api/v1/findings.py` (API), `admin.py` (read-only). Web and API share
the same services and policies; templates, serializers and views contain no business rules.

## Related documents
[finding-lifecycle.md](finding-lifecycle.md) · [finding-detection.md](finding-detection.md) · [findings-security.md](findings-security.md) ·
[findings-operations.md](findings-operations.md) · [api/findings.md](api/findings.md) · ADRs 0063-0065.
