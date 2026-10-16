# API: conditional rules

Base path `/api/v1/checklists/`. Responses use the standard envelope (`{"data": ...}`) and the platform's error format;
authentication is the session (ADR 0033). Authorization is the checklist's: `checklist.view` reads, `checklist.update` writes
(and only the **draft** version). A checklist or rule outside the caller's organizations is `404`; visible but forbidden is `403`.
Rules are created and changed in the draft; a published version's rules are read-only (`400`).

## Metadata
`GET /api/v1/checklists/rules/metadata/` - platform-level (any authenticated, active user; cache it):
```json
{"operators": [{"code": "BETWEEN", "label": "is between", "operand": "range", "description": "...", "families": ["ordered"]}],
 "by_answer_type": {"numeric": ["EQUALS", "NOT_EQUALS", "IS_EMPTY", "IS_NOT_EMPTY", "GREATER_THAN", "..."]},
 "actions": [{"code": "REQUIRE_EVIDENCE", "label": "Require evidence", "target": "item", "property": "evidence",
              "affects_evaluation_order": false, "params": [{"key": "kinds", "kind": "list", "choices": ["photo", "file", "signature"]}]}],
 "limits": {"max_rules_per_version": 200, "max_conditions_per_rule": 25, "max_group_depth": 3}}
```

## Rules

| Method | Path | Notes |
|---|---|---|
| `GET` | `{id}/rules/?version=N` | rules with readable `summary` (default: the draft, else the latest published) |
| `POST` | `{id}/rules/` | create in the draft (`checklist.update`) -> `201` |
| `GET` | `{id}/rules/{rule_id}/` | any version of this checklist |
| `PATCH` | `{id}/rules/{rule_id}/` | draft only; `name`, `description`, `condition`, `actions`, `is_active` (the `code` never changes) |
| `DELETE` | `{id}/rules/{rule_id}/` | draft only -> `204` |
| `POST` | `{id}/rules/validate/` | validate the saved rules or a candidate list; needs `checklist.update` |
| `POST` | `{id}/rules/simulate/` | evaluate for sample answers; **persists nothing** |
| `GET` | `{id}/rules/compiled/` | normalized rules + question metadata for a client-side evaluator |

Create body: `name` and `actions` required; optional `code` (generated `R-001` ...), `description`, `condition` (omit/`null` =
always), `is_active`.
```json
{"name": "Blocked fire exit",
 "condition": {"operator": "AND", "conditions": [{"source_item_code": "FIRE-EXIT", "comparison": "EQUALS", "value": "no"}]},
 "actions": [{"type": "SHOW_ITEM", "target": {"type": "item", "code": "FIRE-ISSUE"}},
             {"type": "REQUIRE_COMMENT", "target": {"type": "item", "code": "FIRE-EXIT"}, "prompt": "Say why"}]}
```
A rule is returned with `code`, `condition` and `actions` in their **normalized** form (values canonicalised, a bare condition
wrapped in an AND group), `sort_order`, `is_active`, `version` and `summary` ("If “Is the fire exit accessible?” equals No, ...").

### Validation errors
`400` with `details` keyed by `condition`, `actions`, `name` ... and an `issues` list of structured findings:
```json
{"error": {"code": "VALIDATION_ERROR", "message": "The request could not be processed; see details.",
  "details": {"condition": ["“GREATER_THAN” is not available for Yes / No questions. Use one of: EQUALS, ..."],
              "issues": [{"level": "error", "code": "invalid_operator", "rule": "R-001", "path": "condition.conditions[0].comparison", "message": "..."}]}}}
```
Cycles, conflicts, unknown references and limit breaches come back the same way. A change that would break rules (for example
deleting a question a rule uses) is refused with the same format on the items endpoints.

## Validate
`POST {id}/rules/validate/` with an optional `version` and an optional `rules` list of candidate rules (the same shape as create,
with an optional `code`); without `rules` it validates what is saved. The response is `200` with the report - findings are data here:
```json
{"valid": false, "errors": [{"level": "error", "code": "cycle", "message": "These rules depend on each other in a circle: ...", "rule": "R-001"}],
 "warnings": [{"level": "warning", "code": "possible_conflict", "message": "Rules R-001 and R-002 can both apply to NOTE; when they do, hiding wins."}]}
```

## Simulate
```http
POST /api/v1/checklists/{id}/rules/simulate/
{"version": 1, "answers": {"FIRE-EXIT": "no", "FRIDGE-TEMP": "7.5"}, "not_applicable": ["EXTRA-NEEDED"], "rules": [ ...optional unsaved rules... ]}
```
* `answers` - raw values by question code (what a client would submit; numbers as strings or numbers, options by value,
  multiple choice as a list). `not_applicable` - codes declared N/A. `rules` - optional **unsaved** candidate rules, replacing
  the version's rules for this call (needs `checklist.update`; saved rules only need `checklist.view`).
* Bounded: at most `max_simulation_answers` answers, `max_simulation_value_chars` per value, `max_simulation_payload_bytes`
  of body (`413` above that). Nothing is stored; no activity is logged.

Response (`simulation: true` labels it):
```json
{"simulation": true, "version": 1,
 "items": {"FIRE-ISSUE": {"visible": true, "required": true, "comment_required": false, "comment_prompts": [], "evidence": null,
                          "options": null, "constraints": null, "instructions": [], "messages": [],
                          "answer": {"status": "empty", "value": null, "errors": []}}},
 "sections": {"EXTRA": {"visible": false}},
 "rules": [{"code": "R-001", "name": "...", "enabled": true, "matched": "true"}],
 "messages": [{"severity": "warning", "message": "...", "rule": "R-002", "target": {"type": "item", "code": "FRIDGE-TEMP"}}],
 "errors": [], "completion": {"applicable_required": 4, "unanswered_required": ["FIRE-ISSUE"], "invalid": [], "complete": false},
 "trace": [{"target": "item:FIRE-ISSUE", "property": "visibility", "value": true, "because": ["SHOW_ITEM by R-001"]}]}
```
`matched` is `"true"`, `"false"` or `"unknown"` (not enough answers - see [rule-operators.md](../rule-operators.md#missing-and-unusable-answers)).
`answer.status` is `answered`, `empty`, `not_applicable`, `invalid` or `hidden`.

## Compiled rules (frontend contract)
`GET {id}/rules/compiled/?version=N` returns what a client needs to evaluate locally: the normalized `rules` (enabled flag,
order, condition, actions), each question's `section`, `answer_type`, normalized `config`, `required`, `active` and `options`,
and the `sections`. **The server is the source of truth**: a client may evaluate for responsiveness using the documented
algorithm ([conditional-rules.md](../conditional-rules.md#evaluation), test vectors in
[rule-conformance-vectors.json](../rule-conformance-vectors.json)), but the server re-evaluates on submit and its result wins.
Hidden answers are preserved, not deleted.

## Imports
Not an HTTP endpoint: `apps/checklists/imports.py::import_rules` / `export_rules` (all-or-nothing, row-level errors); see
[conditional-rules.md](../conditional-rules.md#import-export-and-seeding).
