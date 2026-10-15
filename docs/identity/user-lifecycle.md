# User lifecycle

Every person is one `accounts.User` row. There is no second user table and no separate identity
database.

## The model

| Field | Meaning |
| ----- | ------- |
| `email` | Login identifier and contact address; unique (case-insensitive at lookup) |
| `status` | **Authoritative** lifecycle: `pending`, `active`, `suspended`, `inactive` |
| `is_active` | Stored **mirror** of `status`, kept in sync by `save()`; read by Django |
| `entra_object_id` | The Entra object id (`oid`) - the external identity key, unique |
| `entra_tenant_id` | The Entra directory (`tid`) that vouched for the identity |
| `display_name`, `first_name`, `last_name`, `job_title` | Profile, synchronised from Entra |
| `phone_number`, `avatar` | Application-managed; editable from the account pages |

`status` is authoritative and `is_active` is a mirror because Django's own backends and mixins
read `is_active`, while the product needs to know *why* an account cannot sign in. `User.save()`
derives `is_active = (status == active)`, and a PostgreSQL `CheckConstraint`
(`user_status_matches_is_active`) rejects any row where they disagree. `UserManager.create_user`
translates the legacy `is_active=False` into `status=inactive`.

Helpers: `can_sign_in` (the single sign-in predicate), `is_pending`, `is_suspended`,
`is_inactive`, `is_entra_linked`. Prefer these over comparing a field to a string in a view.

## States

```
                 provision (AUTH_PROVISIONING_MODE)
 unknown ─────────────────────────────► pending ──approve──► active
                                          │                    │
                          AUTH_PROVISIONING_MODE=active         │ suspend
                                          ▼                    ▼
                                        active ◄──reactivate── suspended
                                                                 │ deactivate
                                                                 ▼
                                                              inactive
```

* **pending** - provisioned on first sign-in, not yet approved; cannot sign in.
* **active** - may sign in and use the application.
* **suspended** - temporarily blocked; can be reactivated.
* **inactive** - deactivated; retained for history (nothing is hard-deleted, ADR 0014).

## Provisioning

On a verified sign-in from an unknown identity, `services.provision_user` creates the account:

* with `status = AUTH_PROVISIONING_MODE` (`pending` by default, `active` for a trusted
  deployment), `entra_object_id`, `entra_tenant_id` and the profile claims;
* with an **unusable local password** (Microsoft holds the credential);
* and logs `user.created` (plus a history entry).

The account is persisted **before** the pending sign-in is refused, so an administrator has
something to approve. That ordering is why `resolve_entra_user` raises the denial *after* the
transaction commits.

## Linking and synchronisation

* **Match first by `entra_object_id`** (globally unique). A linked account is refreshed.
* Otherwise match **by email**, subject to `AUTH_EMAIL_LINKING`:
  * `non_staff` (default) - link a first-time identity only to a non-privileged account;
  * `all` - link to any matching account;
  * `disabled` - never link by email.
  A privileged (staff/superuser) account is therefore never attached by a matching email, which
  would otherwise let a mailbox confer administrator rights.
* A mismatched `object_id`, disallowed tenant, missing email or email clash each raise a distinct
  reason code and are refused.
* **Synchronisation** (`sync_user_identity`) writes only Entra-owned fields (names, job title,
  tenant, and email when it is not already taken by someone else). It never touches `status`,
  memberships, staff flags or scope - so a successful sign-in cannot silently reactivate a
  suspended account.

## Administrator operations

The admin edits `status` (never `is_active`, which is read-only) and offers bulk actions that
call the service layer:

* **Approve** selected users → `active`
* **Suspend** selected users → `suspended`
* **Deactivate** selected users → `inactive`

Services (`services.set_user_status`, `suspend_user`, `activate_user`, `deactivate_user`) validate
the transition, mirror `is_active`, and log `user.*` activity - so validation and audit happen
exactly once, whether the call comes from the admin, a view or the API.

## Audit

`django-simple-history` records per-field changes and `ActivityLog` records the business events
(`user.created`, `identity.linked`, `user.updated`, `user.suspended`, `user.activated`,
`user.deactivated`, `organization_membership.created`, …), each with actor, target and metadata.

## Memberships

`OrganizationMembership` (organization-owned, in `apps.organizations`) links a user to an
organization with `is_active` and `is_default`. Membership services
(`add_membership`, `set_default_organization`, `remove_membership`) are idempotent and logged;
an organization is never deleted to remove a user, the membership is.
