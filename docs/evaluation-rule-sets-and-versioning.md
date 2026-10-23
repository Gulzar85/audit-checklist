# Evaluation rule sets: schema, lifecycle and versioning

## Lifecycle

A rule set has the lifecycle of its **checklist version**: *draft* (editable) -> *published* (immutable, sealed) -> *superseded* (retired, kept). *Validated* is not a stored state: validation runs on demand (editor, `validate` endpoint) and always at publication.

1. Open a **draft** version in the builder and choose **Evaluation** (`evaluation.configure`; viewing needs `evaluation.view`).
2. Add rules. Every change saves into the draft and is logged; a draft may hold incomplete rules (kept exactly as typed and flagged).
3. **Try it** with sample inputs - the same engine as real executions; nothing is stored, no action runs.
4. **Publish** the version (`checklist.publish`). Any rule error blocks publication and names the rule.
5. A **new draft** starts with a copy of the rules; change it there and publish again. Executions already started and every recorded evaluation keep the rules of the version they used.

To publish a version **without** automated evaluation, configure nothing or switch evaluation **off**: its executions then report "not evaluated", never a pass.

## Immutability (defence in depth)

Services refuse a non-draft (`409 version_immutable`); a trigger forbids insert/update/delete once the version is not a draft, freezes the version and organization, and keeps the organization equal to the template's; the rule set is part of the version's SHA-256 integrity hash (`verify_checklist_integrity` detects a changed sealed rule set). A version without rules hashes exactly as before this phase. The checklists maintenance override is the only exception and is detected by the hash. The admin is read-only.

## Rule schema

```json
{"rules": [{
  "code": "LOW-SCORE", "name": "Low score review", "description": "", "enabled": true, "priority": 20,
  "condition": {"logic": "AND", "conditions": [
      {"source": "score", "field": "status", "operator": "EQUALS", "value": "final"},
      {"source": "score", "field": "percentage", "operator": "LESS_THAN", "value": "70"}]},
  "outcome": "review_required", "message": "Check the cleaning records.", "on_unknown": "skip", "stop": false,
  "actions": [{"type": "request_finding", "category": "quality", "severity": "medium", "title": "Score below target"},
              {"type": "tag", "tag": "low-score"}]}]}
```

| Key | Meaning |
|---|---|
| `code` | 1-40 letters, digits, `.` `-` `_`; unique in the set; permanent |
| `priority` | whole number 0-100000, **unique**; lower runs first |
| `condition` | a comparison or a group (`AND`, `OR`, or `NOT` with exactly one child); see [conditions](evaluation-conditions-and-semantics.md) |
| `outcome` | `pass`, `warning`, `review_required`, `fail` |
| `on_unknown` | `skip` (recorded, not matched) or `indeterminate` (the evaluation becomes INDETERMINATE unless something definite outranks it) |
| `stop` | after this rule matches, later rules are recorded as skipped |
| `actions` | up to 5 of `request_finding` (category, severity codes of the organization, title, optional description, `requires_review`), `start_workflow` (workflow code of an active workflow for `execution` targets), `tag` |

Unknown keys, sources, fields, operators and action types are errors; values are exact (numbers are decimal text; floats and booleans where numbers are expected are refused).

## Publication validation

Errors (block publishing): malformed rule/document; unknown/invalid code, name, priority, outcome, `on_unknown`, `stop`, `enabled`; duplicate rule **code** or **priority**; no condition; condition problems (unknown source/field/question, an operator not valid for the field's type, an invalid or out-of-vocabulary value, too deep, too many comparisons, wrong `NOT` arity); action problems (unknown type, missing/invalid fields, a category/severity/workflow that does not exist or is inactive in the organization); more than 50 rules. Warnings: no enabled rule; a rule without a message. Each issue names its rule (`subject: rule:CODE`).

## Reference data at run time

Categories, severities and workflows are checked at publication and **again when the action runs** (a later deactivation makes that action *fail visibly*; it never creates a differently classified finding and never changes the evaluation or the submission).

## Retirement and history

Superseding a version retires its rules for new executions of newer versions only; executions of the old version are still evaluated (and re-evaluated) with the rules they were started under. Runs are append-only; a re-evaluation retires the previous *current* run (kept) and appends a new one.
