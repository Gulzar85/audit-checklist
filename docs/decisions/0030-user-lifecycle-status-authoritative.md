# 0030. User lifecycle: one authoritative status field, `is_active` as a mirror

- **Status:** Accepted
- **Date:** 2026-10-15
- **Relates to:** [0011](0011-organization-lifecycle-single-status.md) (organizations), [0006](0006-custom-user-entra-ready.md) (the custom user model)

## Context

Django's `is_active` boolean already gates `authenticate()`, the `ModelBackend`, and
`LoginRequiredMixin`, and Phase 0's `UserManager` used it. But the product needs a real
lifecycle (`pending` / `active` / `suspended` / `inactive`) with distinct, safe messages, and
a pending account must be created *before* an administrator approves it - which means
"not active" cannot be expressed by a single boolean without losing which reason applies.

Two fields that can disagree (`is_active=False` while `status="active"`) would be a security
bug that reads correctly in every code path.

## Decision

- `status` (`UserStatus`) is the **authoritative** lifecycle field. `is_active` is kept purely
  as a **stored mirror** because Django's own backends and mixins read it.
- `User.save()` derives `is_active = (status == active)` and adds `is_active` to
  `update_fields`, so a partial save cannot leave the two out of sync.
- A **database `CheckConstraint`** (`user_status_matches_is_active`) rejects any row where they
  contradict. The invariant therefore holds at the model *and* PostgreSQL level, not just in
  the code paths we happened to route through a service.
- `UserManager` translates legacy intent: `create_user(..., is_active=False)` (with no status)
  yields `status=inactive`, so existing Django conventions keep meaning what they meant.
- `can_sign_in` is the single predicate for "may this account authenticate"; everything else
  (middleware enforcement, policy hints, templates) asks it rather than re-deriving from a
  boolean. Properties `is_pending` / `is_suspended` / `is_inactive` / `is_entra_linked` give
  views and admin a vocabulary that avoids `user.role == "..."`-style checks.
- The admin edits `status` only; `is_active` is read-only there, with lifecycle actions
  (`approve` / `suspend` / `deactivate`) that call `services.set_user_status`.

## Consequences

- One field drives messages, gating and audit; the second field can never contradict it because
  the database refuses the row.
- A migration backfilled existing rows from `is_active` before the constraint was added.
- Django's built-in inactive-user protections (backend, `LoginRequiredMixin`) continue to work
  unchanged, while the UI and logs can still say *why*.
