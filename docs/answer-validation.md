# Answer validation

Entry points (`apps/checklists/answer_types`):

```python
validate_answer(item, raw, *, required=None, not_applicable=False, organization=None, version=None) -> AnswerResult
validate_in_context(AnswerContext(...), raw, *, not_applicable=False) -> AnswerResult   # pure, no database
```

`AnswerResult`: `status` (`answered` | `empty` | `not_applicable` | `invalid`), `value` (normalized Python value),
`serialized` (JSON-safe), `errors` (`code`, `message`, optional `path`), `.valid`, `.as_dict()`.

## Required, optional, not applicable
| Situation | Result |
|---|---|
| required item, empty value | `invalid`, code `required` |
| optional item, empty value | `empty` (valid, no value) |
| `not_applicable=True` | `not_applicable` (valid, no value); whether N/A is *allowed* for an item is a rule decision (Phase 6) made by the caller |
| a value | validated by the type and normalized |

"Empty" is `None`, `""`/whitespace, `[]`, `{}` (and, for a matrix, a dict of empty cells). `False` and `0` are answers.
`yes_no_na` accepts `na` as a *value*, so a required item can still be answered N/A. `required=` overrides the item's own
flag - the hook for the conditional rules engine.

## Normalized value contract
| Type | Accepts | Normalized | Serialized |
|---|---|---|---|
| boolean | `true/false`, `"true"/"false"/"1"/"0"`, `1/0` | `bool` | bool |
| yes_no / yes_no_na | strings, case-insensitive (`N/A`, `n/a` → `na`) | `"yes"/"no"/"na"` | string |
| single_choice | option **value** or option **id** of *this item* | the option value | string |
| multiple_choice | list of option values/ids | values in option order, duplicates collapsed | list |
| score | int, integral float/string; never bool | `int` | number |
| numeric | int, float, decimal string; never bool, NaN, Infinity | `Decimal` quantized to `decimals` | decimal **string** |
| text / long_text | string | stripped; `long_text` newlines normalized to `\n` | string |
| date | `date` or `YYYY-MM-DD` string | `date` | `"2026-10-08"` |
| datetime | ISO 8601 string / datetime | aware `datetime` in UTC | ISO 8601 with offset |
| photo / file | list (or one) of `{file_id, name, size, content_type?}` | same, `content_type` set by the server | same |
| signature | `{signer_name?, file_id?}` | `{signer_name, file_id}` | same |
| matrix | `{row_key: column_key}` or `{row_key: [column_keys]}` | rows in definition order | same |

* **Numeric** uses `Decimal`, never binary floats. More decimal places than allowed is an error (`precision`), not a
  silent rounding; trailing zeros are fine (`4.50` with 1 decimal → `4.5`).
* **Score**: range, then `step` (`(value - min) % step == 0`). An *answer* score only; no totals, percentages or grades.
* **Datetime**: a time without an offset is read in the project's current time zone, then stored in UTC.
* **Duplicates** in a multiple choice collapse (normalization), they are not an error.

## Options and scope
`resolve_option` / `_resolve_option` search only `item.options` (active ones). A value or id belonging to another item,
checklist version or organization simply does not resolve → `invalid_option`. Pass `organization=` and/or `version=`
to also assert the item belongs there (`scope` error otherwise) - defence against forged item ids. Inactive items are
refused (`item_inactive`); an item whose stored type no longer exists returns `unknown_type` instead of crashing.

## Files (contract only)
Photo/file references must have a UUID `file_id` (from the future upload), a safe `name` (no path separators, `..`,
control characters), an allow-listed extension, an integer `size` within `max_size_mb`, and at most `max_files` entries.
A declared `content_type` must match the extension's platform type. No unknown fields; no URLs. Once bytes exist the
evidence phase must call `files.verify_content(extension, header)` (magic numbers; NUL-free text) - the client's MIME
type is never the basis for acceptance. Files must be served only through an authorized view.

## Error codes
`required`, `type`, `invalid`, `invalid_choice`, `invalid_option`, `too_few`, `too_many`, `out_of_range`,
`invalid_step`, `precision`, `too_short`, `too_long`, `multiline`, `characters`, `invalid_row`, `invalid_column`,
`row_required`, `file_id`, `name`, `extension`, `size`, `too_large`, `content_type`, `unknown_field`, `signer_name`,
`signature`, `scope`, `item_inactive`, `unknown_type`.

## Configuration errors vs answer errors
`ConfigurationError` (author mistake, raised by `clean_config`; surfaces as a `config` validation error from the
service/form/API) is distinct from `AnswerInvalid` (user mistake, turned into an `AnswerResult`).
