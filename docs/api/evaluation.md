# Evaluation API

Base `/api/v1/`; envelope, authentication, error format as the rest of v1. Invisible / foreign / malformed id -> `404`; forbidden -> `403`; published rules, not-submitted or concurrent evaluation -> `409`; validation -> `400` (`error.details.code`). Unknown fields are rejected, so a client cannot send an `outcome`, an `evaluation`, an `actions` list or a score. No endpoint writes a result or an action request, and actions cannot be executed directly: only a published rule can request them, and only a stored final run runs them.

| Method | Path | Permission | Purpose |
|---|---|---|---|
| GET | `/evaluation/metadata/` | authenticated | sources and fields, Phase 6 operators, outcomes + precedence, group logics, action types, triggers, limits |
| GET | `/checklist-versions/{id}/evaluation/` | `checklist.view` + `evaluation.view` | `{enabled, document, notes, content_hash, editable, configured, issues}` |
| PUT | `/checklist-versions/{id}/evaluation/` | `evaluation.configure`, draft | save `{enabled?, document?, notes?}`; `409 version_immutable` once published |
| DELETE | `/checklist-versions/{id}/evaluation/` | `evaluation.configure`, draft | remove the rule set |
| POST | `/checklist-versions/{id}/evaluation/validate/` | `evaluation.configure` | `{ok, issues[], rules}` for the saved (or a candidate `enabled` / `document`) rule set; issue = `{level, code, message, subject}` |
| POST | `/checklist-versions/{id}/evaluation/preview/` | `evaluation.configure` | `{answers: {code: value}, not_applicable: [codes], score: {status, percentage, earned, available, grade, critical}, execution_status, enabled?, document?}` -> `{valid, issues, non_production: true, result, would_request[]}`; nothing is stored and no action runs |
| GET | `/executions/{id}/evaluation/` | `evaluation.result_view` (+ `evaluation.evaluate` while in progress) | summary: outcome, trigger, version/rule-set hash, matched rules, undecided rules; provisional while in progress; `{applicable: false, reason}` when not evaluated |
| GET | `/executions/{id}/evaluation/detail/` | `evaluation.explain` | summary plus per-rule results, condition trees, explanations and action outcomes |
| GET | `/executions/{id}/evaluation/runs/` | `evaluation.result_view` | every stored run, newest first |
| POST | `/executions/{id}/evaluation/evaluate/` | `evaluation.evaluate` (in progress) / `evaluation.reevaluate` (submitted) | `{reason}` required for a submitted execution; returns `{..., changed}` |
| GET | `/evaluation-runs/{id}/` | `evaluation.result_view` (`explain` for detail) | one stored run |

Action outcomes (`actions[]`) carry `rule`, `index`, `type`, `status`, `message`, `error`, and a `result` link (`{type: finding|workflow, id, reference, status}`) **only when the caller may see that finding or workflow instance**.

Rule/document shape and examples: [evaluation-rule-sets-and-versioning.md](../evaluation-rule-sets-and-versioning.md), [evaluation-example-rule-sets.md](../evaluation-example-rule-sets.md). Error codes: `malformed`, `too_many_rules`, `unknown_item`, `invalid_status`, `invalid_score`, `reason_required`, `not_evaluated`, `version_immutable`, `template_archived`, `not_submitted`, `concurrent_evaluation`, `invalid_ruleset`.
