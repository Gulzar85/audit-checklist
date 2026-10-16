# Conditional rules and dynamic validation (Phase 6)

Phase 6 lets a checklist *react to its answers*: reveal a follow-up question, require a comment or a photo, show a warning,
tighten a limit, show or hide a whole section. It is **configuration** - a rule is data stored with the checklist version -
and one deterministic evaluator, shared by the builder, the API and (later) execution, decides what it means.

Not in this phase: executing checklists, assignments, storing responses, findings, corrective actions, scoring, evidence
storage, offline sync, notifications, AI, Redis/Celery or an SPA. The rules engine computes an *effective state* from
answers it is handed; it never reads or writes responses. See [the execution contract](#how-execution-will-use-the-engine).

Related: [rule-operators.md](rule-operators.md), [rule-actions.md](rule-actions.md), [rule-validation.md](rule-validation.md),
[api/conditional-rules.md](api/conditional-rules.md), [rule-conformance-vectors.json](rule-conformance-vectors.json),
[ADR 0047](decisions/0047-conditional-rules-engine.md), [answer-types.md](answer-types.md), [checklists.md](checklists.md).

## A rule

```
IF   <condition>                         (a tree of comparisons joined by AND / OR; empty = always)
THEN <action> [; <action> ...]           (1-10 declarative effects on questions or sections)
```

```json
{
  "name": "Blocked fire exit needs detail and a photo",
  "condition": {"operator": "AND", "conditions": [
    {"source_item_code": "FIRE-EXIT", "comparison": "EQUALS", "value": "no"}]},
  "actions": [
    {"type": "SHOW_ITEM", "target": {"type": "item", "code": "FIRE-ISSUE"}},
    {"type": "REQUIRE_COMMENT", "target": {"type": "item", "code": "FIRE-EXIT"}, "prompt": "Say what is blocking the exit"}]
}
```

* Questions and sections are referenced by their **stable codes** (`ChecklistItem.code`, `ChecklistSection.code`), never by
  database id, so a rule survives copying a version and is readable in an export. A reference must exist in the *same
  version*; a code from another checklist or organization simply does not resolve.
* A rule belongs to a **version** and follows its lifecycle: editable in the draft, frozen with the published version, copied
  into the next draft. Rules and the version's structure are validated *together*, so a structure change that would break a
  rule is refused (and publish re-validates everything).
* A condition is a closed shape: `{"operator": "AND"|"OR", "conditions": [...]}` groups (nestable up to a configurable depth)
  and `{"source_item_code", "comparison", "value"}` leaves. Unknown keys are errors - a rule cannot smuggle behaviour.

## Data model

| Model | Fields (Phase 6 additions in bold) |
|---|---|
| `ChecklistSection` | `version`, `name`, `sort_order`, ..., **`code`** (unique per version, `S-001` generated) |
| `ChecklistRule` | `version`, `code` (`R-001`), `name`, `description`, `condition` (JSON, null = always), `actions` (JSON list), `sort_order`, `is_active`; history via `simple_history`; unique `(version, code)` |

Migrations `0003_rules_engine_schema` (adds section codes and the new rule shape; a data step converts Phase 4's single-action
rules and assigns section codes) and `0004_rules_engine_finalize`.

## Architecture

`apps/checklists/rules/` - plain Python, **no database access** outside `snapshot_for_version`:

| Module | Responsibility |
|---|---|
| `snapshot.py` | `VersionSnapshot`: an immutable picture of one version (sections, items with normalized configs and options, rules). Built with a constant number of queries. |
| `operators.py` | The operator registry: codes, which answer-type *families* allow them, operand shape, the comparison function, `clean_operand`. |
| `actions.py` | The action registry: target kind, effective property, parameters, whether it is a graph edge. |
| `schema.py` | `compile_rule`: validates one rule against the snapshot and returns its *normalized* form plus every issue found. |
| `constraints.py` | `SET_VALIDATION_CONSTRAINT`: tighten-only merging through the answer-type engine. |
| `graph.py` | Dependency graph, cycle detection (with a readable path), topological order. |
| `validation.py` | `validate_rule_set`: the single gate (references, values, limits, duplicates, cycles, conflicts). |
| `engine.py` | `compile_rules` + `evaluate`: the evaluator. |
| `describe.py` | Human-readable summaries ("If ... equals No, require a comment on ..."). |
| `builder.py` | The rule builder's form model (posted fields <-> state <-> payload). |
| `limits.py` | Configurable limits (`CHECKLIST_RULE_LIMITS`). |
| `tri.py`, `state.py` | Three-valued logic and the source-answer states. |

Write paths: `services.create_rule | update_rule | set_rule_enabled | delete_rule | reorder_rules | move_rule`. Every channel -
builder, API, import, admin form, seed loader - ends in these, so a rule set is validated the same way everywhere and every
change is audited.

## Evaluation

`evaluate(compiled, answers, not_applicable)` returns the **effective state** for the given answers:

1. Source answers are validated by the Phase 5 answer-type engine (`validate_in_context`). The rules engine never parses an
   answer itself, so "5", `5` and `"5.0"` mean the same thing as they do everywhere else.
2. Nodes (sections, then their questions) are processed in **dependency order**. For each node the *graph-edge* rules that
   target it (show/hide, option availability, validation constraints) are evaluated - their sources are already final - and
   the node's effective answer is recomputed: a hidden question's answer is ignored; an answer outside the effective options
   or limits becomes `invalid`.
3. All other rules are evaluated against the final answer states and annotate the result (required/optional, messages,
   comment and evidence requirements, instructions).

Only a rule whose condition is **TRUE** acts. The result lists, per question: `visible`, `required`, `comment_required` (+
prompts), `evidence`, `options` (available/unavailable), `constraints`, `instructions`, `messages` and the answer's status;
per section `visible`; the rules and whether each matched; the messages in rule order; any `errors`; a `completion` summary
and a `trace` explaining *why* a visibility, option or requirement changed.

### Three-valued logic and missing answers

Conditions evaluate to **TRUE, FALSE or UNKNOWN** (Kleene logic: `AND` is FALSE if any operand is FALSE, else UNKNOWN if any is
UNKNOWN; `OR` mirrors it). A comparison on a source that is not answered - empty, not applicable, invalid or hidden - is
UNKNOWN, **never FALSE**: "not answered" is not "No". Consequently `NOT_EQUALS yes` does not fire on an empty answer, and a
follow-up stays hidden until the answer exists. `IS_EMPTY` / `IS_NOT_EMPTY` are the explicit tests for presence. The full table
is in [rule-operators.md](rule-operators.md#missing-and-unusable-answers); it is tested and in the conformance vectors.

### Conflicts and precedence

Several rules may touch the same target; the outcome never depends on database order:

| Property | Rule |
|---|---|
| Visibility | **Hide beats show.** A target with show-rules is hidden unless one matched; a matched hide always wins. |
| Requirement | **Required beats optional.** |
| Options | Matched restrictions are **intersected** (the most restrictive set). If that would leave no option, all options stay available and an error is reported. |
| Constraints | The **tightest** value of each limit wins; an impossible combination (min above max) applies none and reports a conflict. |
| Messages, instructions | Accumulate, in rule order. |
| Comment / evidence | Any matched rule requires it; kinds are unioned and the largest `min_count` wins. |

Two rules with the *same* condition asking for opposite things of one target are rejected at save time (definite conflict);
different conditions that can both hold are saved with a warning that names the winner.

### Hidden is not deleted
A hidden question's answer is neither validated nor counted for completion, but it is **kept** - revealing the question
again brings it back. The engine never removes answers; the caller decides what to store. N/A is not trusted for a required,
visible question (it is reported invalid), so a client cannot waive a required answer.

## Dependency validation and cycles

Before a rule is saved (and again at publish) the whole rule set is validated: references exist in the version, operators suit
the source's answer type, values parse with the source's own answer-type rules, actions have valid targets and parameters,
limits hold, and there is **no dependency cycle**. Edges are `source -> target` for show/hide, option availability and
validation constraints, plus *section contains item*; a cycle such as "show B when A is Yes" + "show A when B is Yes" is
refused with a readable path (`These rules depend on each other in a circle: A --(R-001)--> B --(R-002)--> A`). Details:
[rule-validation.md](rule-validation.md).

## The builder UI

*Checklist > Builder > Rules* (`/checklists/<id>/rules/`):

* **Rule list** - readable summaries, enable/disable, order, delete, version switcher; read-only for published versions and
  for users without edit rights. A validation panel shows errors and warnings for the whole set.
* **Rule editor** - a visual editor: choose a question and the form offers only the comparisons valid for its answer type and
  the right kind of value control (Yes/No picker, option list, number with unit, date ...); groups with AND/OR; actions with
  per-action controls (message text and style, evidence kinds, option checkboxes, limit fields). A live "in plain words"
  summary and validation appear as you work. Nobody types JSON.
* **Advanced** - the rule as JSON, for power users and for rules nested deeper than the visual editor shows. Loading it runs
  the same validation, and saving always goes through `services.create_rule/update_rule`, so JSON cannot bypass anything.
* **Simulator** (`/checklists/<id>/rules/simulate/`) - enter sample answers and see what the person would see (hidden,
  required, comment/evidence, messages, which rules applied and why). It is clearly labelled a simulation, uses the same
  evaluator, and **persists nothing**.

The editor is server-driven: each HTMX request posts the whole editor, the server applies one operation (add/remove a row,
change a question ...), repairs anything that no longer fits, validates, and returns the fragment - so what you see is what
Save will accept. It uses class-based views, crispy forms, Alpine (CSP build) only for dialogs, and the design system.

## API

`/api/v1/checklists/{id}/rules/` (+ `validate/`, `simulate/`, `compiled/`) and `/api/v1/checklists/rules/metadata/`;
see [api/conditional-rules.md](api/conditional-rules.md).

## Authorization

Rules inherit the checklist's permissions (ADR 0042/0043): `checklist.view` to read rules and run a simulation of the saved
rules; `checklist.update` (and a draft version) to create, change, enable, disable, delete or reorder, to validate candidate
rules and to simulate *unsaved* rules. Out-of-scope checklists, and rule ids that belong to another checklist or organization,
are 404 everywhere (no probing); visible-but-forbidden is 403. No role names are checked.

## Audit

`simple_history` on `ChecklistRule`; ActivityLog verbs `checklist_rule.created | updated | enabled | disabled |
condition_changed | actions_changed | removed | reordered | validation_failed` (each with `metadata.template_id`). A refused
change is logged as `validation_failed` with rule codes and paths only - never user-entered text. Simulation writes nothing.

## Performance and limits

`snapshot_for_version` loads a version with a fixed number of queries (sections, items, options, rules); the list page's
query count does not grow with the number of rules (tested). Evaluation is linear in rules x conditions and never touches the
database. All limits are enforced by validators (not just hidden in the UI) and configurable in
`settings.CHECKLIST_RULE_LIMITS`:

| Limit | Default |
|---|---|
| `max_rules_per_version` | 200 |
| `max_conditions_per_rule` | 25 |
| `max_group_depth` (root = 1) | 3 |
| `max_actions_per_rule` | 10 |
| `max_operand_items` (options in a list operand) | 50 |
| `max_text_operand_length` | 200 |
| `max_message_length` / `max_instruction_length` | 300 / 500 |
| `max_simulation_answers` | 500 |
| `max_simulation_value_chars` | 5000 |
| `max_simulation_payload_bytes` | 200 000 |

## Security

* No `eval`/`exec`, no expression strings: conditions and actions are interpreted from registries; unknown operators, actions
  and keys are rejected. Rule text (names, messages, instructions) is escaped by the templates like any user text.
* Simulation and API inputs are bounded (size, counts, string lengths); answers are just values, whatever they contain.
* Tenant isolation: a rule can only reference codes of its own version; rule ids are always looked up *through* the
  authorized checklist/version; imports and exports re-use the same policies.
* The JSON panel and the API share one validator; CSP is unchanged (no inline script or style was added).

## Import, export and seeding

`imports.import_rules(user=, organization=, rows=)` creates rules from rows (`template`, `name`, `actions` required; `code`,
`description`, `enabled`, `condition` optional; JSON cells). All-or-nothing, each row validated through the service with a
row-numbered error. `imports.export_rules(user=, template=)` writes the same columns (through the authorized selector).
`seed.load_template` accepts section `code` and a `rules` list.

## The demo: Daily Facility Safety Check

`python manage.py seed_demo_checklists --organization <slug>` also loads `DEMO-DFSC` from
`apps/checklists/seed_data/daily_facility_safety.json` - a data file read like any organization's own checklist, with no
code specific to it:

* **Follow-up reveal** - fire exit "No" shows *Describe the fire exit issue* and *Upload a supporting photo* and requires them.
* **Comment requirement** - the same answer requires a comment on the question itself.
* **Temperature warning** - fridge "No" *or* a recorded temperature above 5 or below 0 (an OR with a nested group) shows a warning.
* **Boolean-controlled section** - "Does additional equipment inspection apply?" = Yes reveals the *Additional equipment inspection* section.
* **AND** - both safety checks failing shows an escalation error.

`tests/test_rules_demo.py` runs each scenario through the evaluator.

## How execution will use the engine

The execution phase supplies answers; it must not reimplement the rules.

1. Load the published version's snapshot and `compile_rules` it once (the result is reusable while the rules are unchanged;
   `GET .../rules/compiled/` returns what a client needs to do the same).
2. Call `evaluate(compiled, answers, not_applicable)` on every answer change and **again on the server at submit** - the
   server is the source of truth; a client may evaluate locally for responsiveness (the algorithm is specified here and in
   [rule-conformance-vectors.json](rule-conformance-vectors.json)) but its result is never trusted.
3. Show/hide from `items[...].visible` and `sections[...].visible`; enforce `required`, `comment_required`, `evidence`,
   `options` and `constraints`; show `messages`.
4. **Keep hidden answers** (do not delete them); block completion while `completion.complete` is false.
5. Evidence requirements are *declarations* ("a photo is required"); storing and verifying files is the evidence phase.

## Known limitations / deferred

* The visual editor shows a root group plus one nested level; deeper nesting (allowed by the API up to the configured depth) is
  edited as JSON. Drag-and-drop ordering is not built (move up/down).
* Rules cannot reference answers from other checklists, repeated sections, computed values or dates relative to "today"
  (deliberate: evaluation stays a pure function of the answers).
* `SET_VALIDATION_CONSTRAINT` only tightens; widening a limit by rule is intentionally impossible.
* Evidence and comment requirements are reported; their storage and enforcement of file presence belongs to the execution and
  evidence phases. There is no per-rule scoring (scoring is its own later capability).
* Migrations are generated per environment and not committed (`.gitignore`, [operations](architecture/operations.md)). The
  conversion of Phase 4's single-action rules to the new shape was a data step in the development database's migration; a
  fresh environment has no old rules and simply runs `makemigrations` and `migrate`.
