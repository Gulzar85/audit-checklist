# Identity & access

Phase 2 gives the platform its identity boundary: who a person is (Microsoft Entra ID or a
local password), whether their account may be used (`pending` / `active` / `suspended` /
`inactive`), and which organization they are working in. Authorization proper - roles, scopes,
per-object permissions - is a later phase; this phase establishes the boundary and the lifecycle
only.

The loop is deliberately small and one-directional:

```
Microsoft Entra ID ──(MSAL, ID token)──► resolve_entra_user ──► User (lifecycle)
                                                    │
local password ──(Django auth)──────────────────────┘
                                                    │
                          session ──► active organization (validated)
```

## Contents

Start with [`architecture.md`](architecture.md). Phase 2 reference documents:
[`authentication-flow.md`](authentication-flow.md), [`microsoft-entra.md`](microsoft-entra.md),
[`organization-membership.md`](organization-membership.md), [`session-security.md`](session-security.md),
[`api-authentication.md`](api-authentication.md). The earlier notes below remain as detail.

* [`authentication.md`](authentication.md) - the Entra sign-in flow, configuration, failure
  handling and sign-in throttling.
* [`user-lifecycle.md`](user-lifecycle.md) - the `status` model, provisioning, linking, syncing
  and administrator approval.
* [`organization-context.md`](organization-context.md) - the active organization: how it is
  stored, validated, switched and exposed to templates.
* [`api.md`](api.md) - `/api/v1/me/` and `/api/v1/users/`, and the session-authentication
  posture.

## Layering

`apps/accounts` follows the same five layers as every other domain app:

| Layer | Module | Responsibility |
| ----- | ------ | -------------- |
| Models | `models.py` | `User` persistence + the `status`↔`is_active` invariant |
| Selectors | `selectors.py` | scoped reads (`users_visible_to`, `resolve_active_organization`) |
| Policies | `policies.py` | `can_view_user`, `can_manage_user`, `can_switch_organization` |
| Services | `services.py` | provisioning, linking, sync, lifecycle, memberships, audit |
| Adapters | `views.py`, `admin.py`, `backends.py`, `middleware.py`, `api/v1/identity.py` | adapt only |

Entra-specific plumbing (configuration, MSAL client, claim mapping) is isolated in `entra.py`;
templates never contain auth logic, and no view hard-codes a role check.

## Decisions

The choices behind this phase are recorded as ADRs
[0029](../decisions/0029-msal-for-entra-sign-in.md)-[0034](../decisions/0034-account-appearance-reuses-theme-engine.md) and
[0035](../decisions/0035-membership-status-lifecycle.md)-[0038](../decisions/0038-session-lifetime-and-pwa.md).
