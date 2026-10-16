# API: answer types and checklist items

All responses use the standard envelope (`{"data": ...}`); errors are the platform's consistent error format.
Authentication is the session (ADR 0033); authorization is the same RBAC as the web builder.

## Answer-type metadata
Platform-level metadata - no tenant data - readable by any authenticated, active user (the PWA caches it).

`GET /api/v1/checklists/answer-types/`
```json
[{"code": "score", "label": "Score", "description": "A rating within a range ...", "category": "value", "icon": "star",
  "capabilities": {"requires_options": false, "supports_options": false, "has_fixed_options": false,
                   "supports_comment": true, "supports_evidence": true, "supports_score": true, "supports_rules": true,
                   "produces_scalar": true, "supports_numeric_validation": true, "is_media": false}}]
```
Codes are lower-case (`boolean`, `yes_no`, `yes_no_na`, `single_choice`, `multiple_choice`, `score`, `numeric`, `text`,
`long_text`, `date`, `datetime`, `photo`, `file`, `signature`, `matrix`); the detail endpoint is case-insensitive.

`GET /api/v1/checklists/answer-types/{code}/` adds `config_schema` (per setting: `key`, `label`, `kind`, `default`, `help`,
`min`, `max`, `max_length`, `max_items`, `choices`, `allowed_values`), `default_config` and `example_value`.
Unknown code → `404`.

## Items
Items are created and changed in the checklist's **draft** version. Published versions are read-only (`400`).

| Method | Path | Notes |
|---|---|---|
| `GET` | `/api/v1/checklists/{id}/items/?version=N` | items with options (default: latest published) |
| `POST` | `/api/v1/checklists/{id}/items/` | create (needs `checklist.update`) |
| `GET` | `/api/v1/checklists/{id}/items/{item_id}/` | any version |
| `PATCH` | `/api/v1/checklists/{id}/items/{item_id}/` | draft only |
| `DELETE` | `/api/v1/checklists/{id}/items/{item_id}/` | draft only → `204` |

Create body: `section` (draft section id), `question` (required); optional `code`, `answer_type` (default `yes_no`),
`config`, `is_required`, `is_active`, `description`, `help_text`, `instructions`, `metadata`, `options`.
`options` is a list of labels or `{"value", "label"}` objects (choice types); on `PATCH` it *replaces* the option list
(`set_options`). Example:
```json
{"section": "…", "question": "Record refrigerator temperature.", "answer_type": "numeric",
 "config": {"min": -10, "max": 20, "decimals": 1, "unit": "°C"}, "metadata": {"points": 100}}
```

## Validation and errors
`answer_type`, `config`, `options` and `metadata` are validated by the **service layer** (answer-type registry) - the
serializer only checks shape - so the API behaves exactly like the builder.

| Status | Meaning |
|---|---|
| `400` | invalid payload: field errors such as `{"config": ["“Maximum” must be at most 100."]}`, `{"answer_type": [...]}`, `{"options": [...]}`, `{"metadata": [...]}`; no draft version; section not in the draft |
| `401/403` | not authenticated / lacks `checklist.update` (visible checklist) |
| `404` | checklist, item or section outside the caller's organization (existence is never revealed) |

## Extending
New answer types appear in the list automatically; clients should read `config_schema` rather than hard-coding settings.
