# 0069. Workflow model: states carry the approval stage, versions are sealed, instances bind a version

- **Status:** Accepted
- **Date:** 2026-10-23

## Decision
* **No separate "step" entity.** A stage that waits for people is a *state* with an assignee policy, approval mode, separation options and a deadline; its decisions are the *transitions* leaving it. One concept less to keep consistent.
* **Definition / version / state / transition** are relational rows (no JSON blob of the whole machine). Version states `draft -> published -> superseded -> archived`; one draft and one published version per definition (partial unique indexes).
* **Published versions are immutable at the database**: a trigger freezes every column once a version leaves draft (only the status moves forward) and refuses any change to the states and transitions of a non-draft version, including queryset updates. A SHA-256 `config_hash` of the canonical configuration is stored at publication.
* **An instance is bound to the exact version** it started on (`PROTECT`); publishing a newer version never changes running workflows; migrating an instance to a new version is intentionally not supported.
* **System and organization workflows** share the model (`organization` null = system); an organization default for a target type overrides the system default.
* `entry_seq` (state visits) scopes tasks, approvals and idempotency so loops are safe; `revision` is the instance's optimistic-concurrency token.
* `WorkflowEvent` is an **append-only** table in addition to the activity log: the engine must reason from durable ordered facts (idempotency keys, one approval per actor and visit, replay), which the generic log cannot guarantee.
* `WorkflowTask` carries no simple-history (its `instance` field collides with the historical model's own `instance`); its life is fully recorded by events and the activity log.

## Consequences
A designer edits a draft in small validated steps; reviewers can trust the fingerprint; auditors can reproduce any past workflow from its version.
