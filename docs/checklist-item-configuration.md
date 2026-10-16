# Checklist item configuration

An item stores `answer_type` (a registry code) and `config` (validated, normalized JSON). `config` is the **only**
place type-specific settings live; there are no per-setting columns. `clean_config` rejects unknown keys, wrong types,
out-of-range values and cross-field mistakes (e.g. `min > max`), fills defaults, and is idempotent
(`clean_config(clean_config(x)) == clean_config(x)`). Older rows that predate a key are read with defaults
(`config_with_defaults`), so adding settings never breaks stored data.

## Settings per type
| Type | Key | Kind | Default | Limits |
|---|---|---|---|---|
| single_choice | `display` | `radio`/`dropdown` | `radio` | |
| multiple_choice | `min_select` | int | 0 | 0-100 |
| | `max_select` | int | none | 1-100, ≥ min |
| score | `min` | int | 1 | 0-100 |
| | `max` | int | 5 | 1-100, > min |
| | `step` | int | 1 | 1-100; must divide `max - min` |
| | `default` | int | none | a valid score |
| | `display` | `numeric`/`stars`/`slider` | `numeric` | |
| numeric | `min`, `max` | number | none | ±1e12, `min ≤ max` |
| | `decimals` | int | 2 | 0-6 (0 = whole numbers) |
| | `unit` | text | none | ≤ 20 chars; free text (°C, °F, kg, g, liter, %, minutes, hours ...) |
| text | `min_length` / `max_length` | int | 0 / 255 | ≤ 1000 |
| long_text | `min_length` / `max_length` | int | 0 / 5000 | ≤ 10000 |
| date | `min_date`, `max_date` | ISO date | none | `min ≤ max` |
| datetime | `min_datetime`, `max_datetime` | ISO datetime | none | naive values read in the project time zone, stored UTC |
| photo | `max_files` | int | 1 | 1-10 |
| | `max_size_mb` | int | 10 | 1-50 |
| | `allowed_extensions` | list | jpg, jpeg, png, webp, heic | subset of jpg, jpeg, png, webp, heic, heif |
| | `source` | `any`/`camera` | `any` | |
| file | `max_files`, `max_size_mb` | int | 1 / 10 | as above |
| | `allowed_extensions` | list | pdf, doc, docx, xls, xlsx, csv, txt, jpg, jpeg, png | subset of the platform list (no executables, scripts, HTML, archives) |
| signature | `signer_label` | text | `Signer` | ≤ 80 |
| | `require_name` | bool | true | |
| matrix | `rows`, `columns` | list of labels or `{key, label}` | required | rows ≤ 50, columns ≤ 20, unique, label ≤ 120 |
| | `mode` | `single`/`multiple` | `single` | one column per row, or any columns per row |
| | `require_all_rows` | bool | false | |
| boolean, yes_no, yes_no_na | - | | | no settings; any key is rejected |

Matrix rows/columns are normalized to `{"key", "label"}`; the key (`Dining Area` → `dining_area`) is what answers use
and stays stable if the label is edited. A restaurant checklist, a warehouse dock check and a hospital ward round use the
same matrix shape.

## Options
`single_choice` and `multiple_choice` use `ChecklistOption` rows (stable `value`, editable `label`, explicit order,
`is_active`); ≥ 2 active options are needed to publish. `yes_no` / `yes_no_na` options are created by the platform and
cannot be edited. Services: `create_option`, `update_option`, `delete_option`, `move_option`, and `set_options(item,
[...])` which syncs the whole list in one audited step (API and imports use it).

## Item metadata (ignored by the answer engine)
`ChecklistItem.metadata` holds descriptive facts the answer engine never interprets, validated by
`apps/checklists/metadata.py` with a closed set of keys: `points` (0-10000, source reference points), `critical`
(a failure is an automatic fail in the source form), `applies_to` (tags such as country codes), `reference`. They let
source workbooks be loaded without losing information and are read by the later scoring/rules phases. They are not an
answer score.

## Versions
Configuration belongs to the item *of a version*. A published version is immutable (`version_immutable`); a new draft is
a copy, so changing version 2's range never changes version 1's. The Phase 5 answer path never reads "the latest"
definition - it validates against the specific item it is given.

## Audit
Every change is stored in `simple_history` and the ActivityLog: `checklist_item.updated` (with the field diff),
`checklist_item.answer_type_changed`, `checklist_item.config_changed`, `checklist_option.added|updated|removed|synced`.
