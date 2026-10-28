# KPI catalogue

All KPIs are calculated live from the Phase 19 report named as the source, under the user's scope and the dashboard filters. "Empty scope" is what is shown when nothing is in scope.

| KPI | Definition (numerator / denominator) | Source report | Unit | Empty scope | Previous period |
|---|---|---|---|---|---|
| `assignments_eligible` | assignments issued in the period, not cancelled | completion | count | 0 | yes |
| `assignments_completed` | eligible assignments with status completed | completion | count | 0 | yes |
| `completion_rate` | completed / eligible | completion | % (0.1) | blank (0/0 is not 0%) | yes |
| `assignments_pending` | eligible assignments not started (status assigned) | completion | count | 0 | yes |
| `assignments_overdue` | eligible assignments still open (assigned / in progress) past due; cancelled, completed and draft never count | completion | count | 0 | yes |
| `executions_submitted` | executions started in the period and submitted | execution_summary | count | 0 | yes |
| `average_score` | sum of stored final percentages / scored executions - **one checklist only** | score_summary | % (0.01) | blank; "not comparable" with several checklists | yes |
| `critical_failures` | scored executions with a critical failure | score_summary | count | 0 | yes |
| `findings_created` | findings created in the period | findings_summary | count | 0 | yes |
| `findings_open` | findings open / acknowledged / in progress / reopened now | findings_summary | count | 0 | no (state now) |
| `findings_top_severity` | open findings at the organization's highest-ranked severity (by rank) | findings_summary | count | 0 / blank without severities | no |
| `actions_open` | actions open / in progress / blocked / rejected now | actions_summary | count | 0 | no |
| `actions_overdue` | those of them past their due date | actions_summary | count | 0 | no |
| `actions_awaiting_verification` | actions submitted by the owner, not yet verified | actions_summary | count | 0 | no |
| `workflow_tasks_open` | pending or claimed tasks of active workflows | workflow_summary | count | 0 | no |
| `workflow_tasks_overdue` | those of them past their due date | workflow_summary | count | 0 | no |
| `evaluations_attention` | current evaluations with outcome fail or review required | evaluation_summary | count | 0 | yes |

Widgets (charts and tables) are listed in `apps/dashboards/catalog.py` and by `GET /api/v1/dashboards/{code}/`.
