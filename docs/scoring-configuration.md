# Configuring scoring: lifecycle, formulas, policies and examples

## Lifecycle

1. Open a **draft** checklist version in the builder and choose **Scoring** (permission `scoring.configure`; viewing needs `scoring.view`).
2. Set the **settings**, **score questions** (one rule per question), optionally **section weights**, **grade bands** and **critical failures**. Every change saves into the draft and is logged; a draft may be incomplete.
3. **Try it**: enter sample answers; the saved configuration is scored with the same calculation real executions use. Nothing is stored and no execution is created.
4. **Publish** the version (`checklist.publish`). Publication validates the scoring; errors block it and name the question. The configuration is sealed with the version and can no longer change.
5. A **new draft** starts with a copy of the scoring; change it there and publish again. Executions already started, and every recorded score, keep the rules of the version they used.

To publish a version **without** a score either configure nothing or switch scoring **off**: such an execution shows "not scored", never a score of zero.

## Settings

| Setting | Values | Meaning |
|---|---|---|
| `method` | `percentage`, `raw_total`, `weighted_sections` | see formulas |
| `na_policy` | `exclude`, `zero` | a question answered N/A (the `yes_no_na` answer "N/A" **or** the item-level N/A) is left out, or counts as zero while its points stay available |
| `missing_policy` | `zero`, `exclude` | an unanswered optional question **at submission** counts as zero (points stay available) or is left out. A draft is never penalized: it is *provisional* |
| `precision` | 0-4 | decimals of the percentage (ROUND_HALF_UP) |
| `critical_overrides_grade`, `critical_label` | bool, text | a critical failure replaces the **displayed** outcome with the label; the numbers never change |
| `enabled` | bool | off = the version has no score |

Hidden questions (Phase 6) are **always** excluded; invalid answers are excluded and flagged and keep the result *provisional* - they are never silently zero.

## Question rules (stable codes, exact decimals)

| Answer type | Rule |
|---|---|
| `boolean` | `points: {"true": n, "false": n}` - no answer is assumed to be the good one |
| `yes_no`, `yes_no_na` | `points: {"yes": n, "no": n}`; N/A is **policy**, not points |
| `single_choice` | `points: {option value: n}` for **every** option |
| `multiple_choice` | `points` for every option and `aggregate: "sum"` (add the selected) or `"max"` (highest selected) |
| `numeric` | `ranges: [{"min","max","min_inclusive","max_inclusive","points"}]` - overlaps are errors; gaps are errors for a bounded question and warnings otherwise; a value in no range is excluded and flagged |
| `score` | no mapping: the answer **is** the score; the question's configured maximum is the available points |
| text, long text, date, date-time, photo, file, signature, matrix | **never scored** (a rule on them blocks publication) |

Every rule has a `weight` (0 to 10,000; default 1; 0 lists the question without counting it). Points are 0 to 1,000,000; negative values are refused.

## Formulas

For each included question: `earned = points x weight`, `available = (largest points the question can earn) x weight`.

* **percentage / raw_total:** `percentage = sum(earned) / sum(available) x 100`. `raw_total` merely headlines the points.
* **weighted_sections:** `p_s = sum(earned in s) / sum(available in s)`; `percentage = sum(p_s x w_s) / sum(w_s) x 100` over sections that have available points (`w_s` default 1). Question weights act inside a section only; section weights act between sections only - nothing is weighted twice.
* If nothing is included, or the denominator is 0, the status is **`not_applicable`** (no percentage, no grade) - never 0 %.
* All arithmetic is `Decimal`. Only the percentage is rounded, half-up, to `precision`.

## Grades and critical failures

* **Bands:** `[{"min": "90", "grade": "A", "label": "Excellent"}, ...]`. A band starts at its minimum (inclusive) and runs to the next band; the lowest must start at **0**; minimums are unique and 0-100. The grade is resolved from the **rounded** percentage, deterministically; it never replaces the number. `not_applicable` has no grade.
* **Critical rules:** `{"item", "op": "equals" | "not_equals" | "points_below", "value", "label"}` on a *scored* question; evaluated on visible, answered questions. A hit sets the `critical` flag and is listed with its label; the score is unchanged. With `critical_overrides_grade` the **displayed** outcome becomes `critical_label`.

## Worked example

A checklist with FIRE-EXIT (yes/no, weight 2, yes = 5), HANDWASH (yes/no/N/A, yes = 5) and FRIDGE (numeric 0-10: [0,4) = 0, [4,10] = 5):

```json
{"items": {"FIRE-EXIT": {"weight": "2", "points": {"yes": "5", "no": "0"}},
           "HANDWASH": {"points": {"yes": "5", "no": "0"}},
           "FRIDGE":   {"ranges": [{"min": "0", "max": "4", "max_inclusive": false, "points": "0"},
                                   {"min": "4", "max": "10", "points": "5"}]}},
 "bands": [{"min": "0", "grade": "D", "label": "Needs improvement"}, {"min": "70", "grade": "C", "label": "Satisfactory"},
           {"min": "80", "grade": "B", "label": "Good"}, {"min": "90", "grade": "A", "label": "Excellent"}],
 "critical": [{"item": "FIRE-EXIT", "op": "equals", "value": "no", "label": "Fire exit blocked"}]}
```

Answers: fire exit **yes**, hand-wash **yes**, fridge **3.9**. FIRE-EXIT earns 5 x 2 = 10 of 10; HANDWASH 5 of 5; FRIDGE 3.9 falls in [0,4) = 0 of 5. Earned 15 of 20 = **75 %**, band 70 -> grade **C, Satisfactory**, no critical failure. "Question 1 earned 5 of 5 points. Its weight is 2, so its weighted contribution is 10 of 10." is generated by the engine into the breakdown.
