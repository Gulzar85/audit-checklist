/* The offline rules evaluator: a small, versioned subset of the Phase 6 engine (apps/checklists/rules), for *user experience only*.
 *
 * It evaluates the same declarative rule data the server stores - never code - and must agree with the server on every case in
 * docs/rule-conformance-vectors.json that it supports (tests/rules-engine.test.js).  The server re-evaluates everything at sync and
 * submission; nothing here proves an answer valid.  Checklists that use anything this file does not implement are refused for
 * offline preparation by the server (apps/executions/offline/compat.py), so an unsupported rule is never silently ignored.
 *
 * Semantics (same as the server): three-valued logic (TRUE / FALSE / UNKNOWN) - an unanswered, N/A, invalid or hidden source is
 * UNKNOWN and never fires a rule; visibility actions are evaluated to a fixed point (the server walks a dependency order; for the
 * acyclic rule sets it accepts, both give the same result); hide beats show, required beats optional.
 */
import { normalize, isEmpty } from "./validators.js";

export const SUPPORTED_OPERATORS = [
  "EQUALS", "NOT_EQUALS", "IS_EMPTY", "IS_NOT_EMPTY", "GREATER_THAN", "GREATER_THAN_OR_EQUAL", "LESS_THAN", "LESS_THAN_OR_EQUAL",
  "BETWEEN", "NOT_BETWEEN", "SELECTED", "NOT_SELECTED", "CONTAINS_ANY", "CONTAINS_ALL",
];
export const SUPPORTED_ACTIONS = [
  "SHOW_ITEM", "HIDE_ITEM", "SHOW_SECTION", "HIDE_SECTION", "REQUIRE_ITEM", "MAKE_OPTIONAL", "REQUIRE_COMMENT", "ADD_INSTRUCTIONS", "DISPLAY_MESSAGE",
];

const T = "true", F = "false", U = "unknown";
const ANSWERED = "answered", EMPTY = "empty", NOT_APPLICABLE = "not_applicable", INVALID = "invalid", HIDDEN = "hidden";

const triAnd = (values) => (values.includes(F) ? F : values.includes(U) ? U : T);
const triOr = (values) => (values.includes(T) ? T : values.includes(U) ? U : F);
const tri = (b) => (b ? T : F);

function operand(item, raw) {
  if (item && (item.answer_type === "numeric" || item.answer_type === "score")) {
    return Array.isArray(raw) ? raw.map(Number) : Number(raw);
  }
  return raw;
}

function compute(op, value, rhs, multi) {
  switch (op) {
    case "EQUALS": return value === rhs;
    case "NOT_EQUALS": return value !== rhs;
    case "GREATER_THAN": return value > rhs;
    case "GREATER_THAN_OR_EQUAL": return value >= rhs;
    case "LESS_THAN": return value < rhs;
    case "LESS_THAN_OR_EQUAL": return value <= rhs;
    case "BETWEEN": return rhs[0] <= value && value <= rhs[1];
    case "NOT_BETWEEN": return !(rhs[0] <= value && value <= rhs[1]);
    case "SELECTED": return multi ? value.includes(rhs) : value === rhs;
    case "NOT_SELECTED": return multi ? !value.includes(rhs) : value !== rhs;
    case "CONTAINS_ANY": return multi ? value.some((v) => rhs.includes(v)) : rhs.includes(value);
    case "CONTAINS_ALL": return rhs.every((v) => value.includes(v));
    default: throw new Error(`Unsupported operator ${op}`);
  }
}

function evalCondition(node, states, items) {
  if (!node) {
    return T;
  }
  if (Array.isArray(node.conditions)) {
    const results = node.conditions.map((child) => evalCondition(child, states, items));
    return node.operator === "AND" ? triAnd(results) : triOr(results);
  }
  const state = states[node.source_item_code];
  const op = node.comparison;
  if (!SUPPORTED_OPERATORS.includes(op)) {
    throw new Error(`Unsupported operator ${op}`);
  }
  if (op === "IS_EMPTY") {
    return state.status === EMPTY ? T : [ANSWERED, NOT_APPLICABLE].includes(state.status) ? F : U;
  }
  if (op === "IS_NOT_EMPTY") {
    return state.status === ANSWERED ? T : [EMPTY, NOT_APPLICABLE].includes(state.status) ? F : U;
  }
  if (state.status !== ANSWERED) {
    return U;
  }
  const item = items[node.source_item_code];
  return tri(compute(op, state.value, operand(item, node.value), item.answer_type === "multiple_choice"));
}

function targetOf(action) {
  return action.target ? { kind: action.target.type, code: action.target.code } : null;
}

/** ``answers``: ``{itemCode: raw value}``; ``notApplicable``: item codes declared N/A.  Returns the evaluation (see keys below). */
export function evaluate(pkg, answers = {}, notApplicable = []) {
  const items = Object.fromEntries(pkg.items.map((i) => [i.code, i]));
  const na = new Set(notApplicable);
  const base = {};
  for (const item of pkg.items) {
    if (na.has(item.code)) {
      base[item.code] = { status: NOT_APPLICABLE };
    } else if (!(item.code in answers) || isEmpty(item.answer_type, answers[item.code])) {
      base[item.code] = { status: EMPTY };
    } else {
      const r = normalize(item, answers[item.code]);
      base[item.code] = r.ok ? { status: ANSWERED, value: r.value } : { status: INVALID, errors: r.errors };
    }
  }
  const rules = pkg.rules.filter((r) => r.actions && r.actions.length);
  const edgeKinds = { SHOW_ITEM: "show", HIDE_ITEM: "hide", SHOW_SECTION: "show", HIDE_SECTION: "hide" };
  const declared = {}; // "kind:code" -> [{rule, action, effect}]
  for (const rule of rules) {
    for (const action of rule.actions) {
      if (action.type in edgeKinds && action.target) {
        (declared[`${action.target.type}:${action.target.code}`] ||= []).push({ rule, effect: edgeKinds[action.type] });
      }
    }
  }
  const nodes = Object.keys(declared);
  let itemVisible = Object.fromEntries(pkg.items.map((i) => [i.code, true]));
  let sectionVisible = Object.fromEntries(pkg.sections.map((s) => [s.code, true]));
  let states = { ...base };
  const ruleResult = (rule) => evalCondition(rule.condition, states, items);

  for (let pass = 0; pass <= nodes.length + 1; pass += 1) {
    const nextItem = { ...itemVisible };
    const nextSection = { ...sectionVisible };
    for (const node of nodes) {
      const entries = declared[node];
      const matched = entries.filter((e) => ruleResult(e.rule) === T);
      const hasShow = entries.some((e) => e.effect === "show");
      let visible = !hasShow || matched.some((e) => e.effect === "show");
      if (matched.some((e) => e.effect === "hide")) {
        visible = false;
      }
      const [kind, code] = node.split(/:(.*)/s);
      if (kind === "section") {
        nextSection[code] = visible;
      } else if (code in nextItem) {
        nextItem[code] = visible;
      }
    }
    const effective = Object.fromEntries(pkg.items.map((i) => [i.code, nextItem[i.code] && nextSection[i.section]]));
    const nextStates = {};
    for (const item of pkg.items) {
      nextStates[item.code] = effective[item.code] ? base[item.code] : { status: HIDDEN };
    }
    const changed = JSON.stringify([nextItem, nextSection]) !== JSON.stringify([itemVisible, sectionVisible]);
    itemVisible = nextItem;
    sectionVisible = nextSection;
    states = nextStates;
    if (!changed && pass > 0) {
      break;
    }
  }

  const result = {};
  for (const item of pkg.items) {
    const visible = itemVisible[item.code] && sectionVisible[item.section];
    result[item.code] = {
      code: item.code, section: item.section, visible, baseRequired: item.required, required: item.required, requiredByRule: false, optionalByRule: false,
      commentRequired: false, commentPrompts: [], instructions: [], messages: [], answer: states[item.code],
    };
  }
  const messages = [];
  for (const rule of rules) {
    if (ruleResult(rule) !== T) {
      continue;
    }
    for (const action of rule.actions) {
      if (action.type in edgeKinds) {
        continue;
      }
      const target = targetOf(action);
      const item = target && target.kind === "item" ? result[target.code] : null;
      if (target && target.kind === "item" && (!item || !item.visible)) {
        continue;
      }
      if (target && target.kind === "section" && sectionVisible[target.code] === false) {
        continue;
      }
      if (action.type === "REQUIRE_ITEM" && item) {
        item.requiredByRule = true;
      } else if (action.type === "MAKE_OPTIONAL" && item) {
        item.optionalByRule = true;
      } else if (action.type === "REQUIRE_COMMENT" && item) {
        item.commentRequired = true;
        if (action.prompt) {
          item.commentPrompts.push(action.prompt);
        }
      } else if (action.type === "ADD_INSTRUCTIONS" && item) {
        item.instructions.push(action.text);
      } else if (action.type === "DISPLAY_MESSAGE") {
        const message = { severity: action.severity || "info", message: action.message, rule: rule.code, target };
        messages.push(message);
        if (item) {
          item.messages.push(message);
        }
      } else if (!SUPPORTED_ACTIONS.includes(action.type)) {
        throw new Error(`Unsupported action ${action.type}`);
      }
    }
  }
  for (const r of Object.values(result)) {
    const flag = r.requiredByRule ? true : r.optionalByRule ? false : r.baseRequired;
    r.required = flag && r.visible;
    if (!r.visible) {
      r.commentRequired = false;
      r.commentPrompts = [];
    }
  }
  for (const code of na) {
    const r = result[code];
    if (r && r.visible && r.required) {
      r.answer = { status: INVALID, errors: [{ code: "not_applicable_not_allowed", message: "This question is required and cannot be marked not applicable." }] };
    }
  }
  const visibleItems = Object.values(result).filter((r) => r.visible);
  const unanswered = visibleItems.filter((r) => r.required && r.answer.status === EMPTY).map((r) => r.code).sort();
  const invalid = visibleItems.filter((r) => r.answer.status === INVALID).map((r) => r.code).sort();
  return {
    items: result,
    sections: Object.fromEntries(Object.entries(sectionVisible).map(([code, visible]) => [code, { visible }])),
    messages,
    completion: { unansweredRequired: unanswered, invalid, complete: unanswered.length === 0 && invalid.length === 0 },
  };
}

/** Operational progress, identical to apps/executions/services/progress.py: visible required items (else visible items). */
export function progress(evaluation) {
  const visible = Object.values(evaluation.items).filter((r) => r.visible);
  const required = visible.filter((r) => r.required);
  const basis = required.length ? required : visible;
  const done = basis.filter((r) => [ANSWERED, NOT_APPLICABLE].includes(r.answer.status));
  return { percent: basis.length ? Math.round((100 * done.length) / basis.length) : 0, total: basis.length, answered: done.length, basis: required.length ? "required" : "visible" };
}

/** Which blocking problems would stop submission locally (the server re-checks): per item code, a list of messages. */
export function problems(evaluation, responses = {}) {
  const out = {};
  for (const r of Object.values(evaluation.items)) {
    if (!r.visible) {
      continue;
    }
    const list = [];
    if (r.answer.status === INVALID) {
      list.push(...(r.answer.errors || []).map((e) => e.message));
    } else if (r.answer.status === EMPTY && r.required) {
      list.push("An answer is required.");
    }
    const comment = ((responses[r.code] || {}).comment || "").trim();
    if (r.commentRequired && !comment && (r.required || [ANSWERED, NOT_APPLICABLE].includes(r.answer.status))) {
      list.push(r.commentPrompts[0] || "A comment is required.");
    }
    if (list.length) {
      out[r.code] = list;
    }
  }
  return out;
}
