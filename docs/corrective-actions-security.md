# Corrective actions: organization isolation, RBAC and controls

## Permissions (`apps/permissions/registry.py`)
Location-scoped: `corrective_action.view`, `create`, `update`, `assign`, `reassign`, `start`, `update_progress`, `block`, `submit`, `verify`, `reject`, `close`, `cancel`, `reopen`.
(The proposed `submit_for_verification` is `submit`; history is visible with `view`.) Roles were extended: Administrator and QA Manager hold all; Operations Manager creates, edits,
(re)assigns, works and cancels; Restaurant Manager works (start, progress, block, submit); Auditor views, verifies and rejects. After deployment run `seed_permissions` and `seed_roles`.

## Visibility
`selectors.actions_visible_to(user)` is the only way to list actions: organizations the user may enter x locations where they hold `corrective_action.view` (subtrees expanded), plus actions assigned to them.
Filters/search/sort/pagination apply on top; filter choices come from the same scope. Invisible, foreign or malformed ids are **404**; a visible action the user may not act on is **403**; a repeat or race is **409**.
The **verification queue** is narrower: submitted actions at locations where the user may verify, minus their own work (owner/submitter, and the creator by setting).

## What a client cannot do
Supply or change: the organization, finding (after creation), location, number, status, verifier, creator, submitter, timestamps, decisions or the independence policy - unknown fields are **rejected** (400), not ignored.
Choose an owner who is inactive, from another organization, or not eligible at the location. Verify or reject its own work. Add or edit history (database triggers make updates and decisions append-only).
Bypass the lifecycle via PATCH, admin, forms or imports (there is no import; the admin is read-only).

## Evidence
Reached only through the action (web, API) or the generic attachment endpoints, all authorized by the `corrective_action` target: a *current* view permission at the location (or the current owner with progress permission).
An attachment of any other action is unreachable through this one. Stage rules per purpose are enforced under the action row lock (a submit that wins a race locks further uploads). Quarantined/pending files never count or download.

## Admin
Read-only for actions, updates and verifications (no add/change/delete, no bulk actions); non-superusers see only their scope. Verification and progress history cannot be rewritten even by a superuser through the admin
or by the ORM (`UPDATE`/`DELETE` raise).
