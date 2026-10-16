# 0050. A derived manifest and SHA-256 integrity hash (not a snapshot)

- **Status:** Accepted
- **Date:** 2026-10-16

## Context
The relational rows of a version *are* the version, and the immutability layers (ADR 0049) protect them. Phase 7 still needs (a) a
way to detect that a published definition no longer matches what was published, (b) a way to refuse publishing a draft that
changed after it was validated, and (c) a stable representation to compare versions by code.

## Decision
* No JSON snapshot replaces the relations. `manifest.py` **derives** a canonical manifest (sections -> items -> options, rules)
  from the rows on demand.
* The manifest is canonical JSON (sorted keys, compact separators, UTF-8), lists in definition order, **excluding** primary keys,
  `sort_order` numbers, timestamps, authorship and history. Included fields are listed in the module docstring; template-level
  descriptive metadata is not part of a version definition.
* `SHA-256` of the canonical bytes, stored as `sha256:v<schema>:<hex>` in `ChecklistVersion.integrity_hash` when the version is
  **published** (sealed under the template lock, from the validated state). It is never trusted: `verify_version_integrity`
  re-derives it (`verified / mismatch / unsealed / unverifiable / draft`); the version page, the API and
  `verify_checklist_integrity` expose the result.
* The hash of a **draft** is the **optimistic-concurrency token** of publish (`expected_hash`): a draft that changed after it
  was validated is refused with a conflict. This is why no `revision` column exists - there was no concurrency need the hash
  does not meet.
* The comparison service diffs two manifests structurally; it does not use the hash.
* Schema evolution: a new manifest schema adds a builder and bumps the version in the stored prefix; old hashes keep verifying
  with their builder and are re-sealed only by an explicit maintenance command. `publication_report` (warnings accepted at
  publication, counts) is stored alongside, frozen with the version.

## Consequences
* Tampering that bypassed the guards (superuser, restore) is detectable and demonstrable (`test_the_override...`).
* Versions published before Phase 7 are *unsealed* until `seal_checklist_versions` runs; sealing records the hash of the rows as
  they are then.
* The hash is an integrity aid, not access control. It adds four queries per verification and nothing to ordinary reads.
