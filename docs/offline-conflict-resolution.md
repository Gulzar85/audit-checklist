# Offline conflict resolution

A conflict means the server did **not** apply a local change. The local value is never overwritten and never merged automatically.

| Cause | `code` | Resolvable here? |
|---|---|---|
| The answer changed on the server after this device last synchronized (another device/user) | `revision_mismatch` | yes |
| The execution was already submitted (here or elsewhere) | `execution_submitted` | **no** - the server does not reopen it |
| The execution or assignment was cancelled | `execution_cancelled` / `assignment_cancelled` | no |
| Permission revoked / assignment no longer visible | `forbidden` / `unavailable` (rejected) | no - local data kept |
| The prepared version differs from the assignment's | `stale_version` (rejected) | no - prepare again after exporting |

## Policy

1. Both versions are preserved: the local one in the `conflicts` store, the server's current value/comment/revision in the result.
2. The user sees a comparison (*Mine* / *Server*) and chooses:
   * **Keep the server's answer** - the local value is dropped; the server value becomes the synchronized local value.
   * **Use my answer** - re-sent against the server's *current* revision. The server validates it again (it may be refused, or find a
     new conflict if the server moved again).
   * **Download my answers** - a JSON recovery file; the conflict stays open.
3. Conflicts that cannot be resolved offer only *Download my answers* and an explicit **Discard** (with a warning).
4. No automatic merge, including multiple choice and matrix answers: merging could change the meaning of a checklist response.
5. `POST /api/v1/offline/resolve-conflict/` accepts a resolution only for a conflict **this user received** (stored `conflict` receipt)
   and only for `revision_mismatch`. The resolution has its own operation id (idempotent). Every applied resolution is audited as
   `execution.conflict_resolved` with the resolution and the conflicting operation id.
6. While an answer is in conflict it cannot be edited locally, and the checklist's `submit` is not sent.

## Submitted executions

If the server execution is already submitted, an offline change is a `conflict: execution_submitted`; the execution stays submitted,
the local data is kept and exportable, and the UI explains that the normal offline workflow cannot modify it.

## Authorization revoked while offline

The server applies its *current* policy to every operation. A revoked user gets `rejected: forbidden`/`unavailable` with a generic message
(no data from the server is returned). The local draft is kept; the UI says changes were not accepted. Nothing local overrides the server.
