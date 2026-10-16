# Bulk assignment

Assign **one published checklist version to many locations** in one operation - web (`/assignments/bulk/`) or
`POST /api/v1/assignments/bulk-create/`.

## Policy: all-or-nothing

Every target is validated first - permission **at that location**, organization, active location, version eligibility,
duplicates, the optional responsible user. If **any** target fails, **nothing is created**, and the response lists, per location,
whether it would have succeeded and why not. A partial success never exists, so "all done" can never be misleading, and
repeating a submission is harmless: targets that already have an open (or draft) assignment of that version are reported as
duplicates and the whole request is refused. All writes happen in one transaction; a race that slips past the preflight rolls
everything back and reports again from fresh data. One audit entry `assignment.bulk_created` summarises the batch, besides
each assignment's own `created` / `issued` entries.

Why not "best effort"? Operations managers reconcile by exception; a half-applied rollout is harder to reason about than a refusal
that says exactly what to fix (ADR 0052).

## Choosing targets

* The web form shows a searchable hierarchy picker (location paths such as *Region North / Area 1 / Restaurant 1*) listing **only
  locations the user may assign to**. *Select shown* / *Clear* act on the filtered list.
* **Include descendants** expands each chosen location to its subtree - but only to the descendants the user may assign to. The
  review step reports how many were left out ("N locations beneath your selection are outside your scope").
* Limit: 200 targets per request. Duplicate ids in a request count once.
* Generic refusal: an out-of-scope, unknown, malformed or other-organization location id all yield the same
  "not available" entry. Nothing about it is disclosed.

## Two steps in the web UI

1. **Review** (`stage=review`): a dry run. Nothing is created. Failed targets are highlighted; the confirm button only appears
   when everything would succeed.
2. **Confirm** (`stage=confirm`): runs the service for real, re-validating from scratch. The result page lists the created
   assignments with links.

The API's `dry_run: true` is the same report (HTTP 200); without it a failure is HTTP 400 with the per-location `targets` in
`error.details`.

## Permissions

The caller needs `assignment.bulk_create` (and, when issuing, `assignment.issue`) at **each** target location, plus
`checklist.view` for the version. A user scoped to one area can bulk-assign within that area only.
