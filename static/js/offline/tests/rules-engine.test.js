import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import { evaluate, progress, problems, SUPPORTED_OPERATORS, SUPPORTED_ACTIONS } from "../rules-engine.js";
import { withDefaults } from "../validators.js";

const here = dirname(fileURLToPath(import.meta.url));
const vectors = JSON.parse(readFileSync(join(here, "..", "..", "..", "..", "docs", "rule-conformance-vectors.json"), "utf8")).vectors;

const FAMILY = { text: "text", long_text: "text", datetime: "ordered" };

/** The offline compatibility policy (apps/executions/offline/compat.py) applied to a vector: can the client evaluator run it? */
function supported(vector) {
  const items = Object.fromEntries(vector.items.map((i) => [i.code, i]));
  const walk = (node) => {
    if (!node) return true;
    if (node.conditions) return node.conditions.every(walk);
    if (!SUPPORTED_OPERATORS.includes(node.comparison)) return false;
    if (["IS_EMPTY", "IS_NOT_EMPTY"].includes(node.comparison)) return true;
    return !(FAMILY[items[node.source_item_code].answer_type] || "").length;
  };
  return vector.rules.every((r) => walk(r.condition) && r.actions.every((a) => SUPPORTED_ACTIONS.includes(a.type)));
}

function packageOf(vector) {
  const first = vector.sections[0];
  return {
    sections: vector.sections.map((code, n) => ({ id: `s${n}`, code, name: code })),
    items: vector.items.map((i) => ({
      code: i.code, section: i.section || first, answer_type: i.answer_type, config: withDefaults(i.answer_type, i.config), required: i.required !== false,
      options: (i.options || []).map((v) => ({ value: v, label: v })),
    })),
    rules: vector.rules.filter((r) => r.enabled !== false),
  };
}

const runnable = vectors.filter(supported);

test("the supported subset of the shared conformance vectors is non-trivial", () => {
  assert.ok(runnable.length >= 6, `only ${runnable.length} vectors run`);
});

for (const vector of runnable) {
  vector.cases.forEach((c, index) => {
    test(`vector: ${vector.name.slice(0, 60)} #${index}`, () => {
      const result = evaluate(packageOf(vector), c.answers, c.not_applicable || []);
      const e = c.expect;
      for (const [code, visible] of Object.entries(e.visible || {})) assert.equal(result.items[code].visible, visible, `visible ${code}`);
      for (const [code, required] of Object.entries(e.required || {})) assert.equal(result.items[code].required, required, `required ${code}`);
      for (const [code, visible] of Object.entries(e.sections || {})) assert.equal(result.sections[code].visible, visible, `section ${code}`);
      if (e.messages) assert.deepEqual(result.messages.map((m) => m.message), e.messages);
      if (e.comment_required) assert.deepEqual(Object.values(result.items).filter((r) => r.commentRequired).map((r) => r.code).sort(), [...e.comment_required].sort());
      for (const [code, prompts] of Object.entries(e.comment_prompts || {})) assert.deepEqual(result.items[code].commentPrompts, prompts);
      for (const [code, evidence] of Object.entries(e.evidence || {})) assert.deepEqual(result.items[code].evidence, evidence, `evidence ${code}`);
      for (const [code, status] of Object.entries(e.answer_status || {})) assert.equal(result.items[code].answer.status, status, `status ${code}`);
      if (e.unanswered_required) assert.deepEqual(result.completion.unansweredRequired, e.unanswered_required);
    });
  });
}

const checklist = {
  sections: [{ id: "a", code: "S", name: "S" }],
  items: [
    { code: "EXIT", section: "S", answer_type: "yes_no", config: {}, required: true, options: [] },
    { code: "WHY", section: "S", answer_type: "long_text", config: withDefaults("long_text", {}), required: false, options: [] },
    { code: "TEMP", section: "S", answer_type: "numeric", config: withDefaults("numeric", { min: 0, max: 10 }), required: true, options: [] },
  ],
  rules: [{ code: "R1", condition: { source_item_code: "EXIT", comparison: "EQUALS", value: "no" },
    actions: [{ type: "SHOW_ITEM", target: { type: "item", code: "WHY" } }, { type: "REQUIRE_ITEM", target: { type: "item", code: "WHY" } },
      { type: "REQUIRE_COMMENT", target: { type: "item", code: "EXIT" }, prompt: "Who was told?" }] }],
};

test("progress counts visible required items only", () => {
  assert.deepEqual(progress(evaluate(checklist, {})), { percent: 0, total: 2, answered: 0, basis: "required" });
  assert.equal(progress(evaluate(checklist, { EXIT: "yes" })).percent, 50);
  const blocked = evaluate(checklist, { EXIT: "no", TEMP: "4" });
  assert.equal(progress(blocked).total, 3);
  assert.equal(progress(evaluate(checklist, { EXIT: "yes", TEMP: "4" })).percent, 100);
});

test("an invalid answer is invalid, not answered, and never fires a rule", () => {
  const r = evaluate(checklist, { EXIT: "maybe", TEMP: "99" });
  assert.equal(r.items.EXIT.answer.status, "invalid");
  assert.equal(r.items.TEMP.answer.status, "invalid");
  assert.equal(r.items.WHY.visible, false);
  assert.deepEqual(r.completion.invalid, ["EXIT", "TEMP"]);
});

test("a required comment blocks the local submission check", () => {
  const r = evaluate(checklist, { EXIT: "no", TEMP: "4", WHY: "x" });
  assert.deepEqual(problems(r, {}).EXIT, ["Who was told?"]);
  assert.deepEqual(problems(r, { EXIT: { comment: "Manager" } }), {});
});

test("an unsupported operator is an error, never silently ignored", () => {
  const broken = { ...checklist, rules: [{ code: "X", condition: { source_item_code: "EXIT", comparison: "CONTAINS", value: "a" }, actions: [] }] };
  assert.doesNotThrow(() => evaluate(broken, {})); // no actions: nothing to evaluate
  const used = { ...checklist, rules: [{ code: "X", condition: { source_item_code: "EXIT", comparison: "CONTAINS", value: "a" },
    actions: [{ type: "DISPLAY_MESSAGE", severity: "info", message: "m" }] }] };
  assert.throws(() => evaluate(used, { EXIT: "yes" }), /Unsupported operator/);
});

test("a declared N/A cannot waive a required visible question", () => {
  const r = evaluate(checklist, { TEMP: "4" }, ["EXIT"]);
  assert.equal(r.items.EXIT.answer.status, "invalid");
});
