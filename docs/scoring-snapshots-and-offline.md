# Score snapshots, recalculation, offline behavior and troubleshooting

## Snapshots

A final score is a historical business result. `ScoreResult` stores everything needed to reproduce and explain it: the organization, execution, **exact version**, the **configuration** and its hash, the **algorithm version**, status, earned / available points, percentage, grade, labels, critical flag, the question and section breakdown, the execution revision, and a SHA-256 **input fingerprint** of exactly the answers that were scored. Repeating the calculation with identical inputs, configuration and algorithm gives an identical result.

* Written **once**, at submission, inside the submission transaction (under the execution and assignment locks). Submitting again is a no-op and cannot create a second result.
* **Append-only:** PostgreSQL triggers forbid deleting a result and forbid updating one except switching the *current* flag from true to false when an authorized recalculation appends its successor. There is no API or UI to edit a result, and the admin is read-only.
* **Reproducible:** because versions are immutable and sealed, the configuration a result refers to can always be re-read; if the algorithm changes later, `algorithm` tells which version produced the old number.

## Controlled recalculation

`POST /api/v1/executions/{id}/score/recalculate/` (or the button on the score page) needs `scoring.finalize_recalculate` and a **reason**. The server recomputes under the version's frozen rules:

* identical fingerprint + configuration hash + algorithm -> **nothing changes** (audited as `scoring.recalculation_unchanged`);
* otherwise a new result (`kind = recalculation`, reason, actor) becomes current and the previous one is kept (audited as `scoring.score_recalculated`). Concurrent recalculations are serialized by the execution lock; the partial unique index guarantees one current result.

For an execution still **in progress** the same endpoint (with `scoring.recalculate`) returns a fresh provisional score; nothing is stored.

## Offline synchronization

Offline answers are never trusted as scores. On sync the server (1) authorizes the user and scope, (2) checks the execution and the pinned version, (3) validates and normalizes each answer with the Phase 5 validators, (4) applies the Phase 6 rules, (5) stores the responses, and (6) when the device submits, computes the **final** score on the server inside the submission. Retried operations are deduplicated by the sync receipts; stale or conflicting revisions follow the existing conflict strategy. The sync schema rejects unknown fields, so a payload containing `score`, `grade` or `percentage` is refused.

## Operations

```bash
python manage.py score_executions --dry-run     # how many submitted executions lack their final score
python manage.py score_executions               # create the missing ones (idempotent; never recalculates an existing result)
python manage.py seed_permissions && python manage.py seed_roles   # after deploying Phase 15
```

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| "This scoring cannot be published yet" | open **Scoring**; each message names the question or the part (bands, section, critical rule) |
| An execution shows no score | its version has no scoring or it is switched off ("not scored"), or the submission-time scoring failed - see `scoring.score_failed` in the activity log and run `score_executions` |
| Result is *provisional* after submission | an answer could not be mapped to points (value in no numeric range, invalid answer); it is excluded and listed in the breakdown warnings |
| 403 on the score although the execution is visible | `scoring.score_view` / `scoring.breakdown_view` are separate permissions |
| Percentage differs by 0.01 from a calculator | rounding is half-up on exact decimals at the configured precision |

## Boundaries

A database superuser or the table owner can drop triggers and `TRUNCATE` ignores row triggers; these are administrative controls (database roles, backups). The sealed integrity hash additionally detects a changed configuration (`verify_checklist_integrity`).
