# 0009. Organization identity: UUID keys, an immutable code, a unique slug

- **Status:** Accepted
- **Date:** 2026-10-15

## Context

The organization is the tenant root; every later domain object references it.
Its identifier must survive renames, be safe to expose in URLs and APIs, and be
usable as a business key by imports and integrations.

## Decision

- `id` is a **UUID** primary key (the platform convention). URLs, API payloads
  and every foreign key use it. Nothing public depends on the mutable `name`.
- `code` is the stable, human-friendly **business key**: unique, normalised to
  `UPPER_SNAKE` (`acme-co` → `ACME_CO`), enforced by a database `CHECK` as well
  as validation, and **immutable once created**. Imports key on it.
- `slug` is a unique, readable handle. It is derived from the name when omitted
  and is *not* used in URLs, so it may be edited.
- Normalisation (`Organization.normalize`) runs **before** field validators, so
  `country="gb"` is accepted as `GB` by the UI, API and imports alike.

## Consequences

- Renaming an organization never breaks a reference.
- Two organizations can never collide on a code that differs only by case or
  punctuation.
- Because the code cannot change, an integration that stored it can trust it.
  A typo at creation time needs a data fix, not a form edit.
