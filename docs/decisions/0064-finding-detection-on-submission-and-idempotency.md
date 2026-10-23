# 0064. Finding detection: a Phase 6 action, raised on submission, idempotent by detection key

- **Status:** Accepted
- **Date:** 2026-10-23

## Decision
* **Detection is a Phase 6 action, not a new engine.** `RAISE_FINDING` joins the typed action registry (schema-validated parameters, no code or expressions). The engine only reports
  matched actions on `ItemResult.findings`; it still does no I/O. Conditions, operators, three-valued logic, hiding and N/A semantics are Phase 6's, over Phase 5-validated values.
  The action lives in the rule data of a version, so Phase 7 immutability makes detection behaviour reproducible for historical executions.
* **Findings are raised when an execution is submitted**, inside the submission transaction. Draft answers can change; findings from drafts would be provisional and would need retraction.
  A read-only `preview` exists; the UI promises nothing during a draft. A detection *bug* never loses a submission: it is audited and `detect_findings` retries.
* **Identity**: `detection_key = <execution>:<item>:<rule>#<action index>` with a partial unique constraint. Detection looks up, then inserts in a savepoint and treats a lost
  race as "exists". Numbering is serialised by the organization row lock. Nothing in memory is authoritative.
* **Unknown codes degrade safely**: a rule naming a category/severity the organization lacks raises the finding with the default severity / `other` category flagged `needs_review`
  (a lost finding is worse than a mis-filed one). Codes are resolved at raise time because the rules engine has no database access by design.
* **No withdrawal**: submitted responses are frozen, so triggers cannot change; mistakes are cancelled by a person with a reason.
* The offline JavaScript evaluator (version 3) ignores the action: the server is authoritative.

## Consequences
* Evidence-based conditions work via Phase 9's derived media answers (only available files count).
* Rule authors must keep category/severity codes in step with the organization's settings (surfaced as `needs_review`, never as a failure).
