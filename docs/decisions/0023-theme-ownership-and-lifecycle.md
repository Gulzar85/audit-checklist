# 0023. One Theme model, ownership by scope, a status-driven immutable lifecycle

- **Status:** Accepted
- **Date:** 2026-10-15
- **Builds on:** [0011](0011-organization-lifecycle-single-status.md), [0012](0012-policies-for-authorization.md)

## Context

Three ownership levels are wanted (system, organization, user), with a controlled lifecycle,
safe rollback, an audit trail and no duplicated configuration.

## Decision

**Ownership.** One `Theme` model with `scope` (`system` | `organization`) and a nullable
`organization` (a check constraint ties them together). The *user* level is a **preference**
(`UserAppearance`: light/dark/system), not a theme: users choose a mode, they do not author
palettes. Organizations reference themes by relationship; nothing is copied into `Organization`.

**Identity.** `id` (UUID) is the key; `code` is the stable, immutable, human-friendly
identifier shared by all versions; `name` is only a label. `(organization, code, version)` is
unique (`nulls_distinct=False`, so system codes are unique too).

**Lifecycle.** `draft -> published -> active`; `active -> published` when replaced;
`draft|published -> archived`. *Active is a status*, not a flag: there is no `is_active`
column that could contradict `status = archived`. The database guarantees **at most one active
theme per organization** and one system-wide (partial unique constraints), and a check
constraint requires published/active themes to carry timestamps.

**Immutability and versions.** Only drafts change. Published and active themes are frozen so a
rollback restores *exactly* what ran. A change is a **new version** (same `code`, `version + 1`,
one draft at a time). Cloning gives a new code and leaves the original untouched.

**History.** `ThemeActivation` is an append-only ledger of activation periods (what was active
when, by whom, activated vs rolled back); django-simple-history records every token change
(who changed the primary colour, when, from what to what); `ActivityLog` records
`theme.created|updated|published|activated|archived|cloned|versioned|rolled_back|imported`
and `appearance.preference_changed`.

**Rollback.** Re-activates the most recent previously active theme that is still *published*.
Replaced themes are demoted to *published*, never deleted; archived themes are not restored.

**Authorization.** Separate permissions for designing (`add/change_theme`), publishing
(`publish_theme`) and putting live (`activate_theme`); system scope additionally needs platform
staff. Out of scope is 404, forbidden is 403 (as everywhere).

## Consequences

- Concurrency: lifecycle operations lock the scope (organization row, or an advisory lock for
  the system scope) so two activations cannot interleave.
- Presets are code (`apps/themes/presets.py`), not rows; "from preset" copies overrides into a
  draft. `install_theme_presets` optionally publishes them as system themes.
