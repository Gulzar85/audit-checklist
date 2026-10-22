# Execution submission

`submit(execution, actor)` is one transaction:

1. lock the assignment, then the execution (fixed order);
2. authorize (`execution.submit` at the location and the designation rule) - 403 before any state check;
3. already submitted -> return `already_submitted` (no new event, nothing modified); cancelled -> conflict;
4. verify the assignment is `in_progress` for the same version;
5. rebuild the state from the **stored** responses (the client sends nothing) and compute every blocking problem;
6. problems -> nothing changes; the refusal is audited **outside** the rolled-back transaction
   (`execution.submission_refused`) and a structured error is returned;
7. otherwise stamp rule-hidden answers `is_hidden`, set `submitted` + submitter + time, complete the assignment through its lifecycle
   service, write `execution.submitted` and send `execution_submitted` after commit.

Concurrency: a save and a submission serialize on the execution row. Either the save landed first (and is validated and included)
or it sees a submitted execution and is refused - never half. Simultaneous submissions: one succeeds, the other reports
`already_submitted`.

## After submission

* The workspace redirects to the read-only detail; saves are `409 already_submitted`.
* At the database the trigger refuses any insert, update or delete of the execution's responses and any status change - a queryset
  `update()` or the shell cannot silently edit submitted answers. A superuser who drops the trigger can; that is an operational
  control, documented as a limit.
* The review page (`/executions/<id>/review/`) and `POST /executions/{id}/validate/` run the same checks without submitting.

## Review before submit

The review page lists, from the server's evaluation: progress, blocking problems per question with a link back to the section and
question, and hides the Submit button while anything blocks. Submitting asks for confirmation in a dialog.
