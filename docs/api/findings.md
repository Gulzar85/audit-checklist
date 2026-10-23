# Findings API

Base `/api/v1/`. Envelope and authentication as the rest of v1. Everything is scoped by organization and location
(`findings_visible_to`): invisible / foreign / malformed -> `404`, forbidden -> `403`, repeat or racing request -> `409`, validation -> `400`.
There is no `DELETE`, no `PUT`, no endpoint that writes `status`, and no endpoint that triggers rule evaluation.

## Findings
| Method | Path | Purpose |
|---|---|---|
| GET | `/findings/` | paginated list; filters `status` (incl. derived `overdue`), `severity`, `category`, `source_type`, `location` (+ descendants), `assigned_to`, `detected_after/before`, `due_after/before`; `search`; `ordering` (`number`, `detected_at`, `due_at`, `status`, `severity__rank`, `created_at`) |
| POST | `/findings/` | manual finding (needs `finding.create` at the location) |
| GET | `/findings/{id}/` | one finding, with `allowed_actions`, `can_update`, `can_assign` for the caller |
| PATCH | `/findings/{id}/` | `title`, `description`, `category`, `severity`, `due_at` only |
| POST | `/findings/{id}/acknowledge/` `start/` `resolve/` `close/` `reopen/` `cancel/` | lifecycle; body `{"reason": "..."}` (required for resolve - the resolution summary - reopen and cancel) |
| POST | `/findings/{id}/assign/` | `{"assigned_to": uuid\|null, "due_at"?: datetime\|null}` |
| GET | `/findings/summary/` | counts over the caller's (filtered) scope: `total`, `open`, `awaiting_closure`, `critical`, `overdue`, `needs_review`, `by_status`, `by_severity` |
| GET | `/findings/{id}/history/` | the audit trail, newest first |
| GET | `/findings/{id}/evidence/` | the triggering response's attachments: `id`, `name`, `kind`, `content_type`, `size`, `status`, `available`, `download_url`, `preview_url` (URLs only for servable files) |
| GET | `/findings/{id}/evidence/{attachment}/download/` `preview/` | the bytes; `404` if the attachment is not this finding's, or the file is quarantined/unscanned (preview: images only) |

A "related records" endpoint is intentionally absent: the related execution and checklist version are in the finding's `source` object
(with ids), and the execution is reachable through its own authorized endpoint.

### Create
```json
{"location": "<uuid>", "title": "Wet floor", "description": "...", "category": "<uuid>", "severity": "<uuid>",
 "execution": "<uuid, optional>", "item": "<uuid, optional, needs execution>", "assigned_to": "<uuid, optional>", "due_at": "<datetime, optional>"}
```
Unknown fields (`organization`, `status`, `detected_by`, `source_type`, `number`, `detection_key`, ...) are rejected with `400`. The creator is the caller.
`execution` must be visible to the caller and at the same location; `item` must belong to that execution's version.

### Representation
`id, reference, number, organization, title, description, status, effective_status, is_overdue, source_type, needs_review, rule_code, rule_name,
detected_at, detected_by, due_at, resolution_summary, resolved_at, closed_at, cancelled_at, cancel_reason, reopen_count, created_at,
category{id,code,name}, severity{id,code,name,rank,is_escalation}, location{id,name,path}, assigned_to{id,email,name}|null,
source{assignment, execution, execution_reference, checklist_version, checklist, version_number, item, question, response}|null`.

### Error codes (`error.details.code`)
`invalid_transition`, `reason_required`, `too_long`, `required_field`, `owner_required`, `invalid_assignee`, `invalid_category`, `invalid_severity`,
`invalid_due`, `invalid_source`, `unknown_source`, `not_editable`, `closure_blocked`, `already_<status>` (409), `duplicate` (409).

## Configuration
| Method | Path | Purpose |
|---|---|---|
| GET | `/finding-categories/` `/finding-categories/{id}/` | categories of the caller's organizations (`?is_active=`) |
| POST / PATCH | `/finding-categories/` `/{id}/` | `code`, `name`, `description`, `is_active`, `sort_order`, `organization` (when the caller has several); needs `finding.manage` in that organization; `code` never changes |
| GET | `/finding-severities/` `/{id}/` | severity levels ordered by rank, highest first |
| POST / PATCH | `/finding-severities/` `/{id}/` | `code`, `name`, `rank` (1-100, unique), `requires_owner`, `default_due_days`, `is_escalation`, `is_default`, `is_active`; duplicate rank/code -> `409` |

`is_system` is read-only. Detection-rule configuration is the Phase 6 rules API (`RAISE_FINDING` action, see [rule-actions](../rule-actions.md)).
