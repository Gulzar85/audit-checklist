# 0032. Identity provisioning and linking: default deny, approval before first sign-in

- **Status:** Accepted
- **Date:** 2026-10-15
- **Relates to:** [0012](0012-policies-for-authorization.md) (authorization), [0030](0030-user-lifecycle-status-authoritative.md) (lifecycle), [0014](0014-deactivate-not-delete.md)

## Context

Proving a caller with Microsoft (`oid` + `tid`) is not the same as deciding they may use the
application. The decisions that matter are: how a *new* identity becomes an account, whether an
identity may attach to an *existing* account by email, and what happens when they disagree.

Email is a natural convenience and a natural privilege-escalation vector: if a Microsoft account
whose email matches an existing administrator could attach to it by simply signing in, control
of the mailbox would become control of the platform.

## Decision

- **First sign-in provisions a `pending` account** (`AUTH_PROVISIONING_MODE`, default
  `pending`). The account exists so an administrator can approve it, but `can_sign_in` is false,
  so sign-in is refused with a safe message. `AUTH_PROVISIONING_MODE=active` opts a trusted
  single-tenant deployment into immediate sign-in - explicit, not implicit.
- **The denial is raised after the work commits.** Provisioning/linking and the lifecycle check
  are separate concerns: the account (or link) is persisted first, then `IdentityNotAllowed` is
  raised. Otherwise the exception would roll back the very row an administrator needs to
  approve.
- **Email linking is policy-driven** (`AUTH_EMAIL_LINKING`): `non_staff` (default) links a
  first-time identity to an existing account only when that account is not staff/superuser;
  `all` opts in; `disabled` refuses. A privileged account is never attached by email, so a
  matching address can never inherit administrator rights.
- **`entra_object_id` is the identity key** (globally unique); email is only a matching
  convenience. A mismatched `object_id`, a disallowed `tid`, a missing email, or an email clash
  each raise a distinct, safe reason code mapped to a vague user message and a structured audit
  entry - never provider detail.
- **Synchronisation only touches IdP-owned fields** (display/first/last name, job title, tenant,
  email when not already taken). Status, memberships, staff flags and scope are never written by
  the sync; authentication success never overrides the application's own lifecycle decision.
- Identity operations are the only entry point (`resolve_entra_user` → `provision_user` /
  `link_external_identity` / `sync_user_identity`), all atomic, locked and logged.

## Consequences

- New users require an administrator's approval by default; the admin can approve, suspend or
  deactivate in bulk through services, so validation and audit logging stay in one place.
- A hijack attempt against a privileged account fails closed with an actionable, non-leaking
  message instead of silently granting access.
- Development runs without Microsoft: no tenant/app configured → the flow is simply unavailable
  and the local password fallback applies. There is no hidden production bypass.
