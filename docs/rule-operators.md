# Rule operators

An operator compares a **source question's answer** with a **value** stored in the rule. Operators are data in a registry
(`apps/checklists/rules/operators.py`); which ones a question offers is decided by its answer type's
`comparison_family` (declared by the Phase 5 definition), so a new answer type needs no change here.

## The operators

| Code | Reads as | Value | Families |
|---|---|---|---|
| `EQUALS` | equals | one value | equality, choice, ordered, text |
| `NOT_EQUALS` | does not equal | one value | equality, choice, ordered, text |
| `IS_EMPTY` | is empty | none | all |
| `IS_NOT_EMPTY` | is answered | none | all |
| `GREATER_THAN` | is greater than | one value | ordered |
| `GREATER_THAN_OR_EQUAL` | is at least | one value | ordered |
| `LESS_THAN` | is less than | one value | ordered |
| `LESS_THAN_OR_EQUAL` | is at most | one value | ordered |
| `BETWEEN` | is between (both ends included) | `[from, to]` | ordered |
| `NOT_BETWEEN` | is not between | `[from, to]` | ordered |
| `CONTAINS` | contains | text | text |
| `DOES_NOT_CONTAIN` | does not contain | text | text |
| `STARTS_WITH` | starts with | text | text |
| `ENDS_WITH` | ends with | text | text |
| `SELECTED` | has selected | one option value | choice, multi_choice |
| `NOT_SELECTED` | has not selected | one option value | choice, multi_choice |
| `CONTAINS_ANY` | has selected any of | list of option values | choice, multi_choice |
| `CONTAINS_ALL` | has selected all of | list of option values | multi_choice |

## Which answer types allow which

| Answer type | Family | Operators |
|---|---|---|
| `boolean`, `yes_no`, `yes_no_na` | equality | `EQUALS`, `NOT_EQUALS`, `IS_EMPTY`, `IS_NOT_EMPTY` |
| `single_choice` | choice | the above + `SELECTED`, `NOT_SELECTED`, `CONTAINS_ANY` |
| `multiple_choice` | multi_choice | `IS_EMPTY`, `IS_NOT_EMPTY`, `SELECTED`, `NOT_SELECTED`, `CONTAINS_ANY`, `CONTAINS_ALL` |
| `score`, `numeric`, `date`, `datetime` | ordered | `EQUALS`, `NOT_EQUALS`, `IS_EMPTY`, `IS_NOT_EMPTY`, `GREATER_THAN`(`_OR_EQUAL`), `LESS_THAN`(`_OR_EQUAL`), `BETWEEN`, `NOT_BETWEEN` |
| `text`, `long_text` | text | `EQUALS`, `NOT_EQUALS`, `IS_EMPTY`, `IS_NOT_EMPTY`, `CONTAINS`, `DOES_NOT_CONTAIN`, `STARTS_WITH`, `ENDS_WITH` |
| `photo`, `file`, `signature`, `matrix` | presence | `IS_EMPTY`, `IS_NOT_EMPTY` |

An operator that does not suit the question's type is rejected when the rule is saved (`invalid_operator`), with the list of
allowed ones. `GET /api/v1/checklists/rules/metadata/` returns this table for clients.

## Values

A comparison value is parsed by the **source question's own answer type** (Phase 5), so a rule value follows the same
rules as an answer: a numeric value respects the question's decimal places, a date is `YYYY-MM-DD`, a datetime is ISO-8601
(UTC-normalised), a Yes/No value is `yes`/`no` (`na` for Yes/No/N-A), a boolean is `true`/`false`. The question's own
*range* limits are deliberately not applied - "temperature greater than 25" is a valid rule for a field that only accepts up
to 20. Choice values must be option **values** of that question (an unknown option is rejected). Stored values are normalised
(`"5"` is stored as `"5.0"` for a one-decimal numeric), so the same rule always serialises the same way.

* Range operators need two values with `from <= to`.
* List operators need 1-`max_operand_items` distinct option values.
* Text operands are trimmed, at most `max_text_operand_length` characters, with no control characters.

## Text comparison

`EQUALS`, `CONTAINS`, `DOES_NOT_CONTAIN`, `STARTS_WITH` and `ENDS_WITH` compare after **Unicode NFKC normalisation and
case-folding** and trimming (`"STRASSE"` matches `"straße"`, `"Leak"` matches `"LEAK"`). No wildcards or regular expressions
exist - a text value is always literal.

## Missing and unusable answers

Conditions are three-valued (TRUE / FALSE / UNKNOWN). The state of the source answer decides:

| Source answer is ... | `IS_EMPTY` | `IS_NOT_EMPTY` | every other operator |
|---|---|---|---|
| **answered** (valid) | FALSE | TRUE | the comparison: TRUE or FALSE |
| **empty** (missing, blank) | TRUE | FALSE | **UNKNOWN** |
| **not applicable** (declared N/A) | FALSE | FALSE | **UNKNOWN** |
| **invalid** (fails the answer-type validation) | UNKNOWN | UNKNOWN | **UNKNOWN** |
| **hidden** (its question or section is hidden by a rule, or inactive) | UNKNOWN | UNKNOWN | **UNKNOWN** |

A rule fires only when its condition is TRUE. Hence `NOT_EQUALS` on an empty answer does **not** fire, an `OR` with one
UNKNOWN and one FALSE operand is UNKNOWN, and `AND` with any FALSE operand is FALSE even if another operand is UNKNOWN.
Why: an unanswered question must not trigger "you said No" behaviour; `IS_EMPTY` exists for the case where emptiness *is* the
trigger. Tests: `tests/test_rules_engine.py` (`TestMissingValues`, `TestThreeValuedLogic`) and the conformance vectors.

## Examples

```json
{"source_item_code": "FRIDGE-TEMP", "comparison": "BETWEEN", "value": ["0", "5"]}
{"source_item_code": "HAZARDS", "comparison": "CONTAINS_ALL", "value": ["wet", "blocked"]}
{"source_item_code": "NOTES", "comparison": "CONTAINS", "value": "leak"}
{"source_item_code": "FIRE-PHOTO", "comparison": "IS_NOT_EMPTY"}
```

## Adding an operator
Add an `OperatorDef` (code, label, families, operand shape, compare function) to the registry, add it to this document and to
the conformance vectors. No other module enumerates operators; the builder, API metadata and validation read the registry.
