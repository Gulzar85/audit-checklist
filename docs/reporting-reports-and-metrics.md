# Reports and metric definitions

Each report lists what it counts, its date field and what it deliberately excludes. Status: **implemented** unless noted.

| Report | Source | Date range applies to | Notes |
|---|---|---|---|
| `executions` | `ChecklistExecution` | execution start | exact published version; duration only when submitted; score % / grade and evaluation outcome from the **stored current** snapshots (blank when none or not viewable); answers and evidence are not included |
| `assignments` | `ChecklistAssignment` | assignment creation | **overdue** = open (assigned / in progress) and `due_at` in the past; draft, completed and cancelled are never overdue |
| `completion` (summary) | assignments (+ executions for duration) | assignment issue time | **eligible** = issued and not cancelled (drafts were never issued); completed / in progress / not started / overdue count each assignment **once** (executions are aggregated separately, so retries never inflate totals); completion % = completed / eligible; average duration of submitted executions; grouping: location, checklist, month |
| `scores` | `ScoreResult` (current, final) | execution submission | one row per scored execution; unscored executions are **not listed and never counted as zero**; "snapshot" says submission vs authorized recalculation |
| `score_summary` (summary) | same | submission | scored count, average / lowest / highest %, critical failures; grouping: checklist, location, month |
| `evaluations` | `EvaluationRun` | evaluation time | stored runs incl. re-evaluations (`current` flag/filter); nothing is re-run |
| `findings` | `Finding` | creation | status/severity/category as stored; corrective-action counts by subquery; closure is never inferred from actions |
| `corrective_actions` | `CorrectiveAction` | creation | **overdue** = owner still working (open, in progress, blocked, rejected) and due date passed; owner submission, independent verification and closure are separate dates; verified != finding closed |
| `workflows` | `WorkflowInstance` / `WorkflowTask` | start | status, current state, open tasks, time in state (active instances only), waiting-on (people access) - only recorded data |
| `location_comparison` (summary) | all of the above, per location | period metrics: issue / submission time; "now" metrics: state at generation | assignments (eligible, completed, %, overdue), executions submitted, average stored score, open findings, open and overdue actions, active workflows. Each location counts on its own (no roll-up); locations without data show blanks, not zero percentages |
| `evidence` | - | - | **Blocked**: no bulk, scoped evidence selector exists (see architecture) |

Filters per report are listed in the catalogue and `GET /api/v1/reports/{code}/`. Null handling: nulls are blank cells and sort last.
