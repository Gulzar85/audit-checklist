# Findings: configuration, administration, testing and troubleshooting

## Deployment
1. `python manage.py migrate` (creates `findings_*`, installs the integrity trigger via `post_migrate`).
2. `python manage.py seed_permissions` then `python manage.py seed_roles` (new `finding.*` permissions and role grants).
3. Optional: `python manage.py seed_demo_findings --organization <slug>` (a findings checklist, a submitted execution and examples).
4. Settings: `FINDINGS_REQUIRE_VERIFICATION` (default `false`, see [finding-lifecycle.md](finding-lifecycle.md)).
5. Rebuild assets if you changed templates/JS: `npm run css:build`. The service worker cache is `cp-static-v8`.

## Configuring categories and severities
*Findings -> Settings* (needs `finding.manage`). Defaults (Safety, Quality, Equipment, Process compliance, Documentation, Other;
Low 10, Medium 20 (default), High 30 (needs owner), Critical 40 (needs owner, escalation)) are copied into an organization the first
time it needs them. Codes are permanent (rules refer to them); names, order, owner/due policy and activity can change; nothing is deleted.

## Writing a detection rule
Checklist -> a draft version -> Rules -> action **Raise a finding** (category code, severity code, optional title/description, review
flag, evidence flag), or via the rules API / import. Publish as usual; the published version carries the rule immutably.

## Management commands
| Command | Purpose |
|---|---|
| `detect_findings [--execution EXE-00001] [--organization <slug>] [--since YYYY-MM-DD]` | idempotently (re)run detection for submitted executions - e.g. after a recorded `finding.detection_failed` |
| `seed_demo_findings --organization <slug>` | demo data |

## Testing
```bash
python -m pytest tests/test_findings_detection.py tests/test_findings_lifecycle.py tests/test_findings_security.py tests/test_findings_api.py tests/test_findings_web.py
```
Tests cover detection outcomes and semantics, idempotency and threaded concurrency, the full transition matrix, closure policy,
configuration, authorization/IDOR/isolation, database guards and constraints, API, web, admin, evidence integration and the rule schema.

## Troubleshooting
| Symptom | Likely cause |
|---|---|
| A rule matched but no finding exists | the execution is not submitted yet (findings are raised on submission); or the item was hidden by rules; or see `finding.detection_failed` in the activity log and run `detect_findings` |
| A finding has "needs review" | the rule asked for it, or named a category/severity code the organization does not have (fix the settings or the rule) |
| "Closing requires an independent verification" | `FINDINGS_REQUIRE_VERIFICATION=true` and no verification phase is installed |
| A user cannot be assigned | assignees must hold `finding.resolve` at the finding's location and be active members |
| `403` on settings | `finding.manage` is organization-wide and applies to the *current* organization |

## Known limitations
* Findings are raised on submission only (no provisional findings during a draft).
* Evidence on a finding is the triggering response's evidence (read-through). Uploading *additional* files directly to a finding needs a new Phase 11 target type (documented in ADR 0065) and is not part of Phase 12; manual findings can link an execution/item but cannot upload files yet.
* The UI report form does not yet link an execution/item (the API does).
* No notifications, reminders or escalation (hooks and signals exist); no corrective actions or verification (Phase 13+).
* A cancelled rule-raised finding is never recreated; correcting a wrong cancellation means reporting a new manual finding.
* Rule category/severity codes are resolved when a finding is raised, not when the rule is saved (the rules engine has no database access by design).
