# Workflow definitions, versions, validation and publication

## Lifecycle
`draft -> published -> superseded -> archived`. A definition is created with an empty draft (version 1). **One draft at a time**; creating another draft clones the published version (or the version you name).
Editing (states, transitions, start condition, loops flag, summary) is possible **only on a draft**; publishing validates, supersedes the previous published version (running instances keep it), seals the
version with a **SHA-256 fingerprint** of its canonical configuration (`config_hash`, also written to the audit log) and records who and when. Only a *superseded* version can be archived (and only when nothing runs on it);
archiving a definition stops new starts and is refused while instances are running.

## Immutability (defence in depth)
1. Services refuse a non-draft version with `409 version_immutable`.
2. The API / web forms call only those services; the admin is read-only; there is no import.
3. **Database triggers**: once a version leaves `draft` its columns are frozen (only the status may move forward), and states / transitions of a non-draft version cannot be inserted, updated or deleted - not even by a queryset
   `update()`; versions are never deleted; a version is created as a draft only.

## Publication validation (`validation.py`)
Blocking **errors**: no states; not exactly one initial state; no success end state, or none reachable; a state unreachable from the initial one; a dead end (non-terminal state without an enabled way out); transitions leaving a
terminal state; a loop when `allow_loops` is off; more than one unconditional branch for an (source, action) or an unconditional branch that is not last; an unregistered business action for the target type; a condition
that is malformed, names an unregistered attribute, uses an operator that does not apply, or has an invalid value; an invalid assignee policy (unknown permission, users outside the organization, a system workflow naming users);
`all` approvals without a user-list policy; more minimum approvals than named users; transitions for assignees in a state with no policy; limits (40 states / 120 transitions).
**Warnings**: no failure end state; a conditional action without a fallback branch (an unmatched condition is refused - fail closed); tasks without assignee transitions; an approval mode without an approval transition.

## Conditions (Phase 6 reuse, ADR 0070)
A condition is a leaf `{"attribute", "operator", "value"}` or a group `{"logic": "AND"|"OR", "conditions": [...]}` (depth <= 3, <= 20 comparisons). **Attributes** are those a target type registers (e.g. corrective action:
`priority`, `priority_rank`, `status`, `required_evidence`, `severity`, `severity_rank`, `category`, `has_owner`, `overdue`; finding: `severity`, `severity_rank`, `category`, `status`, `source_type`, `needs_review`, `escalation`, `has_owner`).
**Operators, per-family applicability, missing-value semantics and three-valued logic are the Phase 6 ones** (`apps.checklists.rules.operators` / `tri`); this module only adapts record facts to the Phase 6 answer states.
A missing value, an unknown attribute, a malformed node or an unregistered type is `UNKNOWN`, which never selects a branch and never lets a record start. Nothing is parsed from text; no Python, SQL, JavaScript or expression is ever evaluated.

## Precedence of system and organization workflows
A *system* workflow (no organization; created by platform superusers; read-only for organizations) can be the default for a target type. An organization may define its own default for the same target type: **the organization default wins**,
then the system default. A workflow can also be started explicitly by code (organization first, then system).
