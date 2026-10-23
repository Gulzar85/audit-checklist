# Attachment targets (the controlled registry)

A client never names a Django model, `content_type` or `object_id`. It names a **registered `target_type`** plus opaque references; trusted
server code resolves them. Unknown types, malformed ids, records from another organization and records the caller cannot see all produce the
same generic `404 target_not_found`.

> **Update (Phase 13):** the `corrective_action` target was added following the recipe below (explicit key `EvidenceAttachment.corrective_action` + a `purpose` per stage). The
> evidence services now call the target's `lock`, `touch` and `audit_meta` and filter on `ctx.key`, so a target no longer needs anything from the services. See ADR 0068 and
> [corrective-action-lifecycle.md](corrective-action-lifecycle.md#evidence-stages-purpose).

## The contract (`apps/evidence/targets.py`)

```python
class TargetSpec:
    type = "..."                                    # a value of constants.TargetType
    def resolve(self, user, ref, *, create=False) -> TargetContext   # exists? visible? belongs to the right organization/version?
    def context_for(self, attachment) -> TargetContext               # rebuild the context from a stored attachment
    def can_view(self, user, ctx) -> bool                            # who may see / download the evidence
    def check_can_attach(self, user, ctx) -> None                    # PermissionDenied or TargetLocked (state) - re-run under the row lock
```

`TargetContext` carries the organization, the target objects and what the target accepts (kind hint, allowed extensions, max files).

## Today: `checklist_response`

Reference `{execution_id | assignment_id, item_id}`. Resolution: the execution through `executions.selectors` (an invisible execution is "not
found"), the item must belong to **that execution's version**, be visible per the rules evaluation, and take evidence (a `photo`/`file`/
`signature` question, or a rule currently requiring evidence). `can_view` requires a *current* execution permission at the location (stricter
than Phase 9's "creator keeps access"). `check_can_attach` requires `can_work_at` and an `in_progress` execution
(`409 already_submitted` / `not_editable` otherwise).

## Adding a target type (findings, corrective actions, verifications ...)

1. Add a value to `constants.TargetType`.
2. Add the explicit nullable foreign key(s) to `EvidenceAttachment`, extend the `attachment_*_target_complete` check constraint and the
   `evidence_attachment_guard` trigger (organization consistency), and create the migration.
3. Subclass `TargetSpec`: resolve through that module's **selector** (so invisibility is "not found"), authorize through its **policy**
   (evidence must never widen access), check its lifecycle state, and `register()` it.
4. Extend `selectors.by_item`-style loaders if the module needs evidence in its own answers.

No upload, scanning, storage, API or admin code changes. Deliberately *not* done now: Findings, Corrective Actions and Verifications are
out of scope - only this extension point exists.

## Re-using a file

`services.attach_existing` attaches an already stored, **available** asset to another target after the same resolution and checks (the caller
must be able to see the asset through one of its targets). The physical file is not duplicated.
