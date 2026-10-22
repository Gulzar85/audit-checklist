# IndexedDB schema

Database `checklist-offline`, **version 1** (`static/js/offline/schema.js` → `DB_VERSION`; migrations in `database-migrations.js`).
Access goes through `database.js` (`IdbStorage`): `get`, `getAll`, `getAllByIndex`, `put`, `delete` and an **atomic `batch`** that resolves
only after the transaction committed. `memory-store.js` implements the same interface for the Node tests.

| Store | Key | Indexes | Holds |
|---|---|---|---|
| `meta` | `key` | - | reserved |
| `packages` | `ns|assignmentId` (`…|staging` for an unfinished attempt) | `byNs`, `byAssignment` | the downloaded package, `state` (`preparing`/`ready`/`failed`), `versionId`, `packageHash`, `preparedAt`, `executionId`, `executionStatus`, `submitState`, `blockedReason` |
| `responses` | `ns|assignmentId|itemId` | `byAssignment`, `bySyncState`, `byNs` | the local answer: `status` (`answered`/`not_applicable`/`cleared`), `value`, `comment`, `baseRevision` (last server revision known), `localRevision`, `syncState`, `opId`, `error` |
| `operations` | `opId` | `byAssignment`, `byState`, `byNs` | the outbound queue entry (see below) |
| `conflicts` | `opId` | `byAssignment`, `byNs` | local value, server value, reason, `resolutionOpId` |
| `syncState` | `ns` | - | `seq` counter, `lastSuccessAt`, `lastError` |

Every record carries `ns` = `userId:organizationId`; queries are always namespaced, so one account never reads another's records.

## Operation record

`opId` (UUID, created once, reused on every retry), `type` (`upsert_response`/`clear_response`/`submit`), `seq`, `assignmentId`,
`versionId`, `itemId`, `payload {value, not_applicable, comment}`, `baseRevision` (or `null` + `dependsOn` for an edit chained behind an
in-flight entry), `localRevision`, `state` (`pending`/`sending`/`failed`/`conflict`/`rejected`/`blocked`/`applied`), `attempts`,
`nextAttemptAt`, `lastError`.

## Upgrade rules

1. Upgrades run in the `versionchange` transaction, one function per version step; new versions only **add** stores/indexes or rewrite
   records in place.
2. No upgrade deletes `responses`, `operations` or `conflicts`. A record an upgrade cannot carry forward must be moved into `conflicts`
   for manual recovery.
3. `onversionchange` closes the open connection so another tab can upgrade; `onblocked` is reported as "close the other tabs".
4. Quota errors surface as `StorageFailure("quota")`; other transaction errors as `StorageFailure("storage")`.

## Validation

`validatePackage`, `validateResponseRecord` and `validateOperationRecord` reject malformed data (a tampered or corrupted database) before
it is used. The package hash is an opaque identifier of the download (re-fetch comparison), not proof against local tampering - the
database is untrusted by design.
