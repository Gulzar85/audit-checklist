# Offline API

Base `/api/v1/offline/`. Authentication/envelope as the rest of v1 (`{"data"}` / `{"error"}`); every response here is `Cache-Control: no-store`
except the manifest. Anything outside the caller's organization/scope is **404**/`unavailable`, never a leak.

| Method | Path | Purpose |
|---|---|---|
| GET | `/manifest/` | supported types/operators/actions, limits, schema + evaluator versions |
| GET | `/session/` | `{user_id, organization_id, organization_name, csrf_token}` - identity re-assertion before sync |
| GET | `/assignments/` | paginated open assignments the caller may prepare, each with `offline: {capable, reasons}` |
| GET | `/assignments/{id}/package/` | the exact pinned definition + metadata + saved answers (`409 offline_unsupported` with `reasons`) |
| POST | `/sync/` | apply a batch of operations - see [offline-sync-protocol.md](../offline-sync-protocol.md) |
| POST | `/resolve-conflict/` | resolve a `revision_mismatch` conflict |

There is no asynchronous sync-status endpoint (synchronization is synchronous and transactional per operation) and no endpoint that
touches browser storage.

## Package

```json
{"schema_version": 1, "evaluator_version": 1, "package_hash": "<sha256>",
 "assignment": {"id", "reference", "status", "priority", "due_at", "instructions", "location": {"id", "name", "path"}},
 "checklist": {"template", "name", "code", "version_id", "version_number", "integrity_hash"},
 "sections": [{"id", "code", "name", "description", "instructions", "sort_order"}],
 "items": [{"id", "code", "section", "question", "help_text", "instructions", "answer_type", "config", "required", "sort_order", "options": [{"value","label"}]}],
 "rules": [{"code", "name", "condition", "actions"}],
 "execution": null | {"id", "status", "revision", "responses": [{"item", "status", "value", "comment", "revision"}]},
 "generated_at": "…"}
```

## Sync errors (HTTP)

| Status | When |
|---|---|
| 400 | not an object / no operations / unknown top-level field / more than 200 operations / unsupported `schema_version` / body over 1 MB |
| 401/403 | not signed in / CSRF |
| 429 | throttled (`offline_sync`) |

Per-operation outcomes are in the 200 response (`applied`, `conflict`, `rejected`, `blocked`).

## Resolve conflict

```json
POST /resolve-conflict/
{"operation_id": "<new uuid>", "conflict_operation_id": "<the conflicting op>", "resolution": "keep_server" | "use_local",
 "version_id": "<uuid>", "item_id": "<uuid>", "value": "…", "not_applicable": false, "comment": "…", "base_revision": 2}
```

`use_local` is applied exactly like a normal write (validation, rules, revision check); `keep_server` records the decision. Results:
`applied` (+ `resolution`), `conflict`, `rejected` (`unknown_conflict`, `not_resolvable`, `validation_failed`, …). Audited as
`execution.conflict_resolved`.
