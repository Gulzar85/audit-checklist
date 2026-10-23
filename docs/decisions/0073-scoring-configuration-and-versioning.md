# 0073. Scoring configuration belongs to the checklist version: one sealed row, declarative document, lifecycle hooks

- **Status:** Accepted
- **Date:** 2026-10-23

## Decision
* **The configuration is owned by a checklist version** (`ScoringConfig`, one-to-one) and follows its lifecycle: editable while a draft, frozen when published, sealed by the version's integrity hash. There is no second versioning framework and no separate publication step.
* **One row with a validated JSON document** (`items`, `sections`, `bands`, `critical`) plus scalar settings, instead of one table per concept (question rules, section weights, bands, critical rules). A scoring scheme is always read, validated, sealed, cloned and compared **as a whole**; one immutable row makes the seal and the draft-only trigger trivial and prevents a half-applied configuration. Rules are keyed by stable item / section codes and option values (which clones preserve), so no id remapping exists. The document is declarative, strictly parsed (unknown keys, floats and non-finite numbers are refused) and normalized to canonical decimal text before it is stored.
* **Extension through hooks, not imports:** `apps.checklists.hooks` offers `manifest_extensions`, `publication_validators` and `clone_hooks`; scoring registers in `AppConfig.ready`. A version without scoring contributes nothing to the manifest, so every hash sealed before Phase 15 still verifies (no schema bump). Compare shows scoring through the same manifest.
* **Immutability in depth:** services refuse a non-draft (409), and `scoring_guard_config` forbids insert/update/delete once the version is not a draft, freezes the version and organization, and keeps the organization equal to the template's. The checklists maintenance override (`allow_definition_changes`) is the only exception and is detected by the integrity hash.
* **Scoring on or off:** `enabled = false` (or no configuration) publishes the version without a score; an execution then reports "not scored", never zero.

## Consequences
Publishing a version publishes its scoring atomically; old executions and results keep the rules they used; the JSON document trades per-field SQL constraints for strict application and publication validation (the document is never queried across versions).
