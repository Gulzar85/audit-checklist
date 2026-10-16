# Answer types (Phase 5)

An **answer type** defines *how a checklist item is answered*: what it is called, how it is configured, how a submitted
value is validated, and what the normalized value looks like. The engine lives in `apps/checklists/answer_types/` and
is used identically by the web builder, the REST API, imports and - later - execution, the PWA and offline sync.

The engine defines **how an item is answered**. It does not store answers (execution, Phase 9), evaluate rules
(Phase 6) or compute checklist scores (Phase 15). `Answer Type = SCORE, answer = 4` does **not** mean the checklist has a
score of 80 %; `Answer Type = YES_NO, answer = no` does **not** create a finding. Those are later capabilities that read
answers.

## The fifteen types

| Category | Code | Answer | Configuration |
|---|---|---|---|
| Choice | `boolean` | true / false | - |
| | `yes_no` | `yes` / `no` | - (fixed options) |
| | `yes_no_na` | `yes` / `no` / `na` | - (fixed options) |
| | `single_choice` | one option value | `display` radio/dropdown; options are rows |
| | `multiple_choice` | list of option values | `min_select`, `max_select`; options are rows |
| Value | `score` | whole number in a range | `min`, `max`, `step`, `default`, `display` |
| | `numeric` | decimal | `min`, `max`, `decimals`, `unit` |
| | `text` | single line | `min_length`, `max_length` |
| | `long_text` | multi-line | `min_length`, `max_length` |
| Date & time | `date` | calendar date | `min_date`, `max_date` |
| | `datetime` | aware date-time (UTC) | `min_datetime`, `max_datetime` |
| Media | `photo` | list of file references | `max_files`, `max_size_mb`, `allowed_extensions`, `source` |
| | `file` | list of file references | `max_files`, `max_size_mb`, `allowed_extensions` |
| | `signature` | signer name + optional image reference | `signer_label`, `require_name` |
| Advanced | `matrix` | `{row: column}` (or lists) | `rows`, `columns`, `mode`, `require_all_rows` |

Full schemas, defaults and limits: [checklist-item-configuration.md](checklist-item-configuration.md). The value
contract and validation rules: [answer-validation.md](answer-validation.md). API: [api/answer-types.md](api/answer-types.md).

## Architecture

```
ChecklistItem (answer_type code + config JSON)      <- the database stores only these two facts
        │
        ▼
AnswerTypeRegistry  ──►  AnswerTypeDefinition        <- behaviour in code, one small class per type
        │                   ├─ metadata  (code, label, description, category, icon)
        │                   ├─ capabilities
        │                   ├─ config_fields  (schema: key, kind, default, min/max, choices)
        │                   ├─ clean_config(raw)   -> normalized, storable config
        │                   ├─ normalize(raw, ctx) -> normalized value  (or AnswerInvalid)
        │                   └─ serialize(value)    -> JSON-safe value
        ▼
validate_answer(item, raw) / validate_in_context(ctx, raw)   -> AnswerResult
```

* **No table per type, no column per setting.** An item has `answer_type` and a `config` JSON whose allowed keys, types,
  ranges and defaults are declared by the type's schema (`schemas.ConfigField`). Unknown keys are rejected. The same
  schema drives validation, the builder's dynamic form, the API metadata and these docs.
* **One service path.** `constants.clean_config` (used by `services.create_item/update_item`) delegates to the registry.
  The builder form, the API serializer and the importer never validate on their own: they pass data to the service.
* **DB-free validation.** `AnswerContext` (type + normalized config + the item's options + required flag) is everything
  a validation needs, so the same code is the reference for the PWA's local validation and for re-validation on sync.
* **Capabilities** (`capabilities.py`) are asked from the registry - `requires_options`, `supports_options`,
  `has_fixed_options`, `supports_comment`, `supports_evidence`, `supports_score`, `supports_rules`, `produces_scalar`,
  `supports_numeric_validation`, `is_media` - never hard-coded in a view or template.

## Adding an answer type
1. Add the code to `constants.AnswerType` (database check constraint) - a migration is generated for the constraint.
2. Write a class in `answer_types/definitions.py`: `code`, `label`, `description`, `category`, `icon`, `preview`,
   `capabilities`, `config_fields`, `normalize`, `serialize` (and `check_config` for cross-field rules).
3. Add it to `ALL_DEFINITIONS`; add a `kind == "<preview key>"` branch to `templates/checklists/_answer_preview.html`.
4. Tests: `tests/test_answer_types.py` already checks every registered type against the enum, categories and
   `clean_config` idempotency.
No view, form, serializer or API change is needed: the builder form and API metadata are generated from the schema.

## Builder
The item editor (`/checklists/<id>/builder/sections/<id>/items/new/`) shows a grouped selector (Choice / Value /
Date & Time / Media / Advanced), the type's description, and *only* that type's settings - loaded over HTMX
(`answer-type/fields/`) and built from the schema, so there is no form with every possible field. A live preview
(`answer-type/preview/`) renders disabled controls from the current settings; it is a design aid, not the execution UI.

## Security
Server-side validation is mandatory and central. Option references are resolved only against the item's own options
(another item's, version's or organization's option does not resolve); `organization`/`version` assertions catch forged
item ids; configuration is schema-validated (no unknown keys, bounded sizes); file references accept only allow-listed
extensions, never trust the client's MIME type, and never carry a URL. Labels and help text are plain data and are
HTML-escaped on output. Authorization is Phase 3 RBAC (`checklist.update` on the organization; published versions are
immutable).

## Future integration
Phase 6 passes `required=` overrides and N/A decisions into `validate_answer`; Phase 9 stores `AnswerResult.serialized`
per response; Phase 10 ships `GET /api/v1/checklists/answer-types/` to the PWA for local validation and re-validates on
sync with the same engine; Phase 11 stores uploaded files and calls `files.verify_content`; Phase 15 reads `score`
answers and item `metadata.points` - the engine itself never scores.
