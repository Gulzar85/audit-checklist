# Example rule sets with inputs and expected results

Both examples are executed by `tests/test_evaluation_engine.py::TestDocumentedExamples`, so the results below are the engine's actual results. Question codes are those of the sample checklist (`Q-101` is a Yes/No question, `TEMP` a numeric one, `NOTE` a text one).

## Example A - safety and score

```json
{"rules": [
 {"code": "CRIT", "name": "Critical safety failure", "priority": 10, "outcome": "fail", "message": "Escalate to the QA manager.",
  "condition": {"logic": "OR", "conditions": [
    {"source": "score", "field": "critical", "operator": "EQUALS", "value": true},
    {"source": "answer", "field": "Q-101", "operator": "EQUALS", "value": "no"}]},
  "actions": [{"type": "request_finding", "category": "safety", "severity": "high", "title": "Critical safety failure", "requires_review": true},
              {"type": "start_workflow", "workflow": "exec-review"}]},
 {"code": "LOW", "name": "Low score review", "priority": 20, "outcome": "review_required",
  "condition": {"logic": "AND", "conditions": [
    {"source": "score", "field": "status", "operator": "EQUALS", "value": "final"},
    {"source": "score", "field": "percentage", "operator": "LESS_THAN", "value": "70"}]},
  "actions": [{"type": "request_finding", "category": "quality", "severity": "medium", "title": "Score below target"}]}]}
```

| `Q-101` | Score | Outcome | Matched | Why |
|---|---|---|---|---|
| Yes | final, 85 %, grade B | **PASS** | - | nothing matched |
| Yes | final, 62 %, grade D | **REVIEW_REQUIRED** | `LOW` | “score percentage is less than 70 (observed: 62)”; one quality finding requested |
| **No** | final, 62 %, grade D | **FAIL** | `CRIT`, `LOW` | both matched; FAIL outranks REVIEW_REQUIRED and both stay listed; a safety finding, a workflow and a quality finding are requested |
| Yes | final, 90 %, **critical flag** | **FAIL** | `CRIT` | the score's critical flag alone matches the OR group |
| Yes | no score exists | **PASS** | - | `LOW` is *undecided* (the score is unavailable, never 0 %), so it does not match; add `score.status EQUALS unavailable` with `review_required` if a missing score must be reviewed |

Re-evaluating the third row with identical inputs returns the same run: no second finding, no second workflow.

## Example B - temperature, grade and a note

```json
{"rules": [
 {"code": "HOT", "name": "Temperature too high", "priority": 10, "outcome": "fail",
  "condition": {"source": "answer", "field": "TEMP", "operator": "GREATER_THAN", "value": "8"}},
 {"code": "GRADE", "name": "Grade D needs review", "priority": 20, "outcome": "review_required",
  "condition": {"source": "score", "field": "grade", "operator": "EQUALS", "value": "D"}},
 {"code": "NOTE", "name": "A leak was mentioned", "priority": 30, "outcome": "warning",
  "condition": {"source": "answer", "field": "NOTE", "operator": "CONTAINS", "value": "leak"}, "on_unknown": "skip"}]}
```

| `TEMP` | `NOTE` | Score | Outcome | Matched | Undecided |
|---|---|---|---|---|---|
| 9.5 | “small leak” | final, 75 %, C | **FAIL** | `HOT`, `NOTE` | - |
| 4 | *(not answered)* | final, 55 %, D | **REVIEW_REQUIRED** | `GRADE` | `NOTE` (an unanswered note cannot be searched; recorded, not matched) |
| 4 | “all good” | final, 95 %, A | **PASS** | - | - |

## Building these in the UI

*Checklist builder -> Evaluation -> Add rule*: pick **Compare** (a question's answer / the score / the execution), **What**, **How** (only operators valid for that field appear), and the value; put rows in groups for AND/OR; choose the outcome; add requests (finding with the organization's categories and severities, workflow, tag). **Try it** shows the evaluation of sample inputs, clearly labelled as a non-production preview.
