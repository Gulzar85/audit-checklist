# Scoring API

Base `/api/v1/`; envelope, authentication, error format as the rest of v1. Invisible / foreign / malformed id -> `404`; forbidden -> `403`; published configuration, not-submitted or concurrent recalculation -> `409`; validation -> `400` (`error.details.code`). Unknown fields are rejected, so a client cannot send a score, grade, percentage or total. No endpoint writes a result.

| Method | Path | Permission | Purpose |
|---|---|---|---|
| GET | `/scoring/metadata/` | authenticated | methods, policies, critical conditions, scorable types, limits, defaults |
| GET | `/checklist-versions/{id}/scoring/` | `checklist.view` + `scoring.view` | the configuration `{settings, document, notes, content_hash, editable, configured, issues}` |
| PUT | `/checklist-versions/{id}/scoring/` | `scoring.configure`, draft | save `{settings?, document?, notes?}` (each given part replaces that part); `409 version_immutable` once published |
| DELETE | `/checklist-versions/{id}/scoring/` | `scoring.configure`, draft | remove the configuration |
| POST | `/checklist-versions/{id}/scoring/validate/` | `scoring.configure` | `{ok, issues[]}` for the saved (or a candidate `settings`/`document`) configuration; each issue `{level, code, message, subject}` |
| POST | `/checklist-versions/{id}/scoring/preview/` | `scoring.configure` | `{answers: {code: value}, not_applicable: [codes], final?, settings?, document?}` -> `{valid, issues, result}`; nothing is stored |
| GET | `/executions/{id}/score/` | `scoring.score_view` (+ `scoring.recalculate` for an in-progress execution) | summary: stored final result, or provisional while in progress; `{applicable: false, reason}` when not scored |
| GET | `/executions/{id}/score/breakdown/` | `scoring.breakdown_view` | summary plus `items`, `sections`, `warnings`, `explanation`, `critical_hits` |
| GET | `/executions/{id}/score/history/` | `scoring.breakdown_view` | every stored result, newest first |
| POST | `/executions/{id}/score/recalculate/` | `scoring.recalculate` (in progress) / `scoring.finalize_recalculate` (submitted) | `{reason}` required for a final score; returns `{..., changed}` |

Document shape and examples: [scoring-configuration.md](../scoring-configuration.md). Result fields: `status` (`provisional|final|not_applicable`), `earned`, `available`, `percentage` (exact decimal strings), `grade`, `label`, `display_label`, `critical`, `kind`, `is_current`, `algorithm`, `config_hash`, `execution_revision`, `calculated_at`, `reason`.

Error codes: `malformed`, `unknown_setting`, `invalid_setting`, `unknown_item`, `reason_required`, `not_scored`, `version_immutable`, `template_archived`, `not_submitted`, `concurrent_recalculation`.
