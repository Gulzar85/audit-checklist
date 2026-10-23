# Corrective actions and their parent finding

**Rule of thumb: nothing in the corrective-action module ever resolves, closes, cancels or reopens a finding.** A person does that through the Phase 12 finding service; this
module only supplies a *policy* that the finding service consults through its documented hooks (`apps/findings/hooks.py`).

## Eligibility (`eligibility.finding_eligibility(finding)`)
A finding is **eligible for resolution** when none of its actions is *outstanding* - draft, open, in progress, blocked, awaiting verification or rejected. Cancelled actions do not count;
verified and closed ones are done. A finding with no actions is eligible (actions are optional unless a process requires them). The result is a fact about the finding (all of its actions, not only those a
given user may see): `{eligible, outstanding[], done, cancelled, reasons[]}`; it is shown on the finding and action pages and in the API (`finding_eligibility`, `GET /findings/{id}/corrective-actions/`).

## How the finding service uses it
| Finding action | Hook | Effect |
|---|---|---|
| `resolve` | `resolve_blockers` | refused (`resolution_blocked`) while actions are outstanding - switch off with `CORRECTIVE_ACTIONS_BLOCK_FINDING_RESOLUTION=false` |
| `cancel` | `cancel_blockers` | refused (`cancellation_blocked`) while actions are outstanding: cancel or finish them first |
| `close` | `verification_gate` | with `FINDINGS_REQUIRE_VERIFICATION=true`, closing needs the gate: no outstanding action **and** at least one verified/closed action (`CORRECTIVE_ACTIONS_REQUIRED_FOR_VERIFICATION`) |

Consequences: one verified action does not free a finding that still has another outstanding action; cancelling or reopening an action leaves the finding as it is; reopening a verified action while its finding is
already resolved does not reopen the finding (the finding page shows it is no longer eligible, and a person decides). Creating an action does not change the finding either.

## When actions may be created
For findings that are not closed or cancelled (open, acknowledged, in progress, reopened, resolved). Organization and location are the finding's.
