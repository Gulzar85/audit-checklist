# Finding detection

## The rule (Phase 6 integration)
There is **no second rules engine**. A detection rule is an ordinary Phase 6 rule - a condition tree over normalized answers plus
actions - whose action is `RAISE_FINDING` (see [rule-actions.md](rule-actions.md)):

```json
{"name": "Blocked exit",
 "condition": {"source_item_code": "EXIT", "comparison": "EQUALS", "value": "no"},
 "actions": [{"type": "RAISE_FINDING", "target": {"type": "item", "code": "EXIT"},
              "category": "safety", "severity": "high", "title": "Blocked: {question}",
              "description": "Answered {answer} at {location}.", "requires_review": false, "inherit_evidence": true}]}
```

* **Conditions** are exactly the Phase 6 operators (`EQUALS`, `NOT_EQUALS`, `IS_EMPTY`, `IS_NOT_EMPTY`, `LESS_THAN`, `GREATER_THAN`,
  `BETWEEN`, `SELECTED`, `CONTAINS_ANY/ALL` ...) over the values the Phase 5 validators produced, combined with AND/OR groups
  (see [rule-operators.md](rule-operators.md)). Boolean/yes-no, choice, multi-choice, score, numeric, safe text operators and
  evidence presence (a photo/file question's answer is derived from its *available* attachments, so `IS_EMPTY` means "no verified evidence")
  all work through the same code path.
* **The action is a typed schema**: `category` and `severity` are organization codes; `title`/`description` may use only
  `{question}`, `{answer}`, `{location}`, `{checklist}` (plain substitution - never `format`, `eval`, SQL or code); unknown parameters
  are refused. It is validated when the rule is saved and immutable once the version is published (Phase 7), so the behaviour of a
  published checklist is reproducible for every historical execution.
* The rule builder (web), the rules API and checklist import/export all carry the action; the JavaScript offline evaluator
  (version 3) understands and **ignores** it - a device never creates an authoritative finding.

## Semantics (documented, tested)
| Situation | Result |
|---|---|
| condition TRUE on a visible item | finding raised |
| answer missing / unanswered optional item | condition UNKNOWN (or an explicit `IS_EMPTY` rule) - nothing raised unless a rule says so |
| N/A | not equal to `no`; raises nothing unless a rule explicitly tests it |
| invalid answer | cannot be submitted; raises nothing |
| item hidden by rules (or section hidden) | the finding is **not** raised; the answer is kept and ignored, as in Phase 9 |
| rule names a category/severity code the organization does not have | the finding is raised with the organization's default severity / `other` category and flagged `needs_review` (never silently lost) |
| two `RAISE_FINDING` actions match the same item | one finding per `(rule, action index)` |

## Timing: findings are raised on submission
Findings are created **inside the submission transaction** (`ExecutionSubmissionService`, after the execution is marked submitted),
because draft answers can still change; a finding raised from a draft would be provisional and would have to be retracted. The
in-progress workspace therefore promises nothing; `services.preview(execution)` (read-only, not exposed as a promise) shows what the
current answers *would* raise. Offline executions are submitted through the same Phase 9 service by the Phase 10 sync, so they
raise findings identically when the server accepts the submission.

If detection itself fails unexpectedly, the **submission is not lost**: the failure is recorded (`finding.detection_failed`) and
`manage.py detect_findings` raises what was missed.

## Idempotency and duplicate prevention
* Identity: `detection_key = <execution id>:<item id>:<rule code>#<action index>`, with `UNIQUE(organization, detection_key)`.
* Detection looks the key up first, then inserts inside a savepoint; a lost race on the unique constraint returns the winner's finding.
* The organization row is locked while numbers are allocated, serialising concurrent detection and manual creation.
* Repeated runs (retries, the command, an offline resubmission) return `exists`; the outcome is `created | exists | none`.
* No in-memory state is involved - the database is the source of truth. Verified by threaded tests.

## When the trigger changes
Submitted responses are frozen (Phase 9), so a triggering answer cannot change afterwards and nothing is ever "withdrawn"
automatically. A finding raised in error is **cancelled by an authorized person with a reason** (history kept); it is not recreated.

## Evidence
`inherit_evidence` findings show the triggering item's live attachments **read-through** (no file is copied). Access is re-checked
through the finding; only `available` files are served or counted - quarantined and unscanned files are listed with their state and never served.
