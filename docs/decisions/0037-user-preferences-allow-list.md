# 0037. User preferences: an allow-listed key/value store; appearance stays in the theme engine

- **Status:** Accepted
- **Date:** 2026-10-15
- **Relates to:** [0034](0034-account-appearance-reuses-theme-engine.md), [0025](0025-controlled-token-validation.md)

## Context
Users need a few personal choices (time zone, language) without a column per future preference, and
without duplicating the theme engine's appearance setting.

## Decision
`UserPreference(user, key, value JSON)`, unique per (user, key). Only keys registered in
`apps/accounts/preferences.py` are accepted, each with its own validator (`timezone` IANA name,
`language` code); unknown keys are errors. Writes go through `set_preference` (audited as
`user.preference_changed` with from/to). Appearance remains `themes.UserAppearance`; the
`/api/v1/me/preferences/` document and the preferences page expose both, delegating appearance to the
theme service. New preferences = one registry entry, no migration.

## Consequences
The store cannot become a free-form bucket; theme data is never duplicated onto the user.