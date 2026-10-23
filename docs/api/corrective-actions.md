# Corrective actions API

Base `/api/v1/`. Envelope and authentication as the rest of v1. Scoped by organization and location: invisible / foreign / malformed -> `404`, forbidden (permission, ownership, separation of duties) -> `403`,
repeat or racing request -> `409`, validation -> `400`. No `DELETE`, no `PUT`, and no way to write `status`, the verifier, the creator or any decision field.

## Records
| Method | Path | Purpose |
|---|---|---|
| GET | `/corrective-actions/` | list; filters `status` (incl. derived `overdue`), `priority`, `finding`, `location` (+descendants), `assigned_to`, `verification` (`approved`/`rejected`), `due_after/before`, `created_after/before`; `search`; `ordering` (`number`, `due_at`, `created_at`, `updated_at`, `status`, `priority_order`) |
| POST | `/corrective-actions/` | create against `finding` (needs `corrective_action.create` at the finding's location, a visible and open finding, an eligible owner) |
| GET / POST | `/findings/{id}/corrective-actions/` | the finding's actions + its `eligibility`; create under it (the finding is the URL's) |
| GET | `/corrective-actions/{id}/` | one action with `allowed_actions`, `can_edit`, `can_assign`, `can_add_update`, `finding_eligibility`, `verifications[]` |
| PATCH | `/corrective-actions/{id}/` | `title`, `description`, `plan`, `expected_outcome`, `completion_criteria`, `priority`, `due_at`, `required_evidence` (the owner: plan text only) |
| GET | `/corrective-actions/{id}/history/` | the audit trail, newest first |
| GET / POST | `/corrective-actions/{id}/updates/` | the work log; POST `{"body", "progress_percent"?, "corrects"?}` appends a progress note or a correction |

## Lifecycle (`POST`, body `{"reason": "..."}` where needed)
`issue/`, `start/`, `block/` (reason = the blocker), `resume/`, `submit-for-verification/` (reason = completion summary), `verify/` (reason = optional notes), `reject/` (reason required), `close/`, `cancel/` (reason required),
`reopen/` (reason required), `assign/` and `reassign/` (`{"assigned_to": uuid|null}`; first assignment needs `assign`, a change needs `reassign`). See [../corrective-action-lifecycle.md](../corrective-action-lifecycle.md) for the matrix.

## Evidence (Phase 11 services)
| Method | Path | Purpose |
|---|---|---|
| GET | `/corrective-actions/{id}/evidence/` | attachments with `purpose`, `available`, `download_url` (only for servable files); `?include_detached=1` |
| POST | `/corrective-actions/{id}/evidence/` | multipart `purpose` (`plan\|progress\|completion\|verification\|rejection`), `file`, `caption?`, `operation_id?`, `client_sha256?` - `201`, or `200 duplicate` for an idempotent repeat |
| GET | `/corrective-actions/{id}/evidence/{attachment}/download/` `preview/` | the bytes after re-checking the action and the attachment's belonging; `404` if quarantined/unscanned |

## Worklists and summary
`GET /my-corrective-actions/` (also `/corrective-actions/mine/`) - actions assigned to the caller (not drafts, not finished);
`GET /corrective-actions/verification-queue/` - submitted actions the caller may decide (needs `corrective_action.verify` somewhere; never their own work);
`GET /corrective-actions/summary/` - `total`, `open`, `in_progress`, `blocked`, `awaiting_verification`, `rejected`, `overdue`, `verified`, `by_status` over the caller's filtered scope.

## Error codes (`error.details.code`)
`invalid_transition`, `reason_required`, `too_long`, `required_field`, `owner_required`, `invalid_assignee`, `evidence_required`, `invalid_due`, `invalid_priority`, `invalid_progress`, `invalid_correction`, `invalid_kind`,
`protected_field`, `unknown_finding`, `finding_not_open`, `not_editable`, `already_<status>` (409), `already_decided` (409).
