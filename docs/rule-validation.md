# Rule validation

One function, `validate_rule_set(snapshot)` (`apps/checklists/rules/validation.py`), decides whether a version's rules are
acceptable. It is the **only** gate: the builder, the API, imports, the admin form, the seed loader, structure changes and
publish all call it (through the services), so a rule set that passes here is safe to evaluate.

```python
report = validate_rule_set(snapshot_for_version(version))   # or snapshot.with_rules([... candidate ...])
report.ok, report.errors, report.warnings, report.compiled   # compiled = normalized, trusted rules
```

Findings are `Issue(level, code, message, rule, path)`: `level` is `error` (blocks saving/publishing) or `warning`
(informational), `rule` the rule code, `path` where in the rule (`condition.conditions[1].value`, `actions[0].target.code`).
**All** problems of a rule are collected, not just the first.

## When validation runs

| Moment | What is validated | On failure |
|---|---|---|
| Create a rule | the version's whole set with the new rule | `RuleValidationError` (nothing saved) |
| Update a rule | the whole set with the changed rule | same |
| Enable a rule | the whole set (a disabled rule may hide a cycle) | same |
| Change or remove a question, option or section a rule uses; change a question's type/config/active flag | the set after the change | the change is refused: "*Removing FIRE would break the rules of this version. Update or remove the rule first.* ..." |
| Publish a version | the whole set, plus the structural checks | publish refused, problems listed (`Rule R-001: ...`) |
| Import / admin save / seed | each row/rule through the same services | row-numbered or field errors |
| `POST .../rules/validate/` | the saved rules or a *candidate* list, without saving | a report, never an error status |

A refused change is recorded as `checklist_rule.validation_failed` (rule codes and paths only, no user text).

## What is checked

| Code | Level | Meaning |
|---|---|---|
| `invalid_rule` | error | a rule has no action, a bad shape, or a name problem |
| `invalid_condition` | error | a condition/group is malformed or has unexpected keys |
| `unknown_item` | error | a source or target question/section code is not in **this version** |
| `invalid_operator` | error | the comparison is not valid for the source question's answer type (the allowed ones are listed) |
| `invalid_value` | error | the comparison value does not parse with the source question's own answer type |
| `invalid_action` | error | unknown action type, wrong target kind, bad/unknown parameters, illegal constraint, an option restriction that leaves nothing |
| `too_deep`, `too_many` | error | a configured limit (group depth, conditions or actions per rule, rules per version) was exceeded |
| `duplicate_code` | error | two rules share a code |
| `cycle` | error | the dependency graph contains a cycle (see below) |
| `conflict` | error | a *definite* conflict: two enabled rules with the **same condition** (or one rule) ask for opposite things of the same target (show/hide, required/optional) or contradictory limits |
| `possible_conflict` | warning | different conditions that can both hold ask for opposite things; the documented precedence will apply (hide beats show, required beats optional ...) and the message names the winner |
| `inactive_source`, `inactive_target` | warning | an enabled rule reads or targets an inactive question (it can never match / has no visible effect) |

Disabled rules are validated for shape and references but excluded from cycle and conflict checks until enabled.

## Dependency graph and cycles

Nodes are questions and sections. Edges (only from **enabled** rules, and only for actions that change whether another
rule's *source answer counts* - the "graph edge" actions of [rule-actions.md](rule-actions.md)):

* `source question -> target` for every show/hide, option-availability and validation-constraint action of a rule whose
  condition reads the source;
* `section -> each question it contains` (hiding a section hides its questions).

Annotation-only actions (require, optional, message, comment, evidence, instructions) add no edge: they cannot influence any
condition, so they cannot create a cycle. A cycle makes evaluation order undefined, so it is rejected:

```
These rules depend on each other in a circle: DT “Lane clear?” --(R-002)--> FIRE “Is the fire exit accessible?”
--(R-001)--> DT “Lane clear?”. Remove or change one of the rules.
```

A self-reference (a rule that reads and changes the same question) is a one-step cycle. If a cyclic set ever reached the
evaluator, it refuses to apply *any* rule and reports `cycle` - it never loops.

## Conflicts

See [conditional-rules.md](conditional-rules.md#conflicts-and-precedence) for the run-time precedence. At save time:

* same condition, opposite actions on one target -> `conflict` (error): the author clearly contradicted themselves;
* different conditions -> `possible_conflict` (warning): legitimate (e.g. "show when A" and "hide when B"), the winner is stated;
* limits that cannot all be satisfied together -> `conflict`/`possible_conflict` depending on whether the conditions are identical.

## The evaluator also reports

`errors` in an evaluation include any validation errors of the rule set, `unknown_item` for answers to unknown questions,
`options_exhausted`, `constraint_conflict` and `cycle` - an evaluation never raises because of a bad rule set.

## Limits
Configured in `settings.CHECKLIST_RULE_LIMITS`; see [conditional-rules.md](conditional-rules.md#performance-and-limits).

## Testing
`tests/test_rules_engine.py` (operators, three-valued logic, actions, conflicts, dependency validation, cycles, completion),
`tests/test_rules_services.py` (services, structure changes, publish, versions, audit), `tests/test_rules_api.py`,
`tests/test_rules_web.py`, `tests/test_rules_admin_import.py`, `tests/test_rules_demo.py` and
`tests/test_rules_conformance.py` (language-neutral vectors).
