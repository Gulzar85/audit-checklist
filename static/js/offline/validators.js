/* Local (preliminary) answer validation.  The server is authoritative: this gives immediate feedback and normalizes the value into the
 * shared answer contract (apps/checklists/answer_types) - the same shapes the server accepts and stores.  A value that passes here can
 * still be refused by the server (and then shows as a sync rejection); a value refused here is never sent.
 */

const CONTROL = /[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f]/;
const DEFAULTS = {
  numeric: { decimals: 2 },
  score: { min: 1, max: 5, step: 1 },
  text: { min_length: 0, max_length: 255 },
  long_text: { min_length: 0, max_length: 5000 },
};

/** Fill the defaults the server would (a package already has them; test fixtures may not). */
export function withDefaults(answerType, config) {
  return { ...(DEFAULTS[answerType] || {}), ...(config || {}) };
}

const ok = (value) => ({ ok: true, value, errors: [] });
const bad = (code, message) => ({ ok: false, value: null, errors: [{ code, message }] });

export function isEmpty(answerType, raw) {
  if (raw === null || raw === undefined) {
    return true;
  }
  if (answerType === "boolean") {
    return typeof raw === "string" && raw.trim() === "";
  }
  if (typeof raw === "string") {
    return raw.trim() === "";
  }
  if (Array.isArray(raw)) {
    return raw.length === 0;
  }
  if (typeof raw === "object") {
    return Object.keys(raw).length === 0;
  }
  return false;
}

function decimalsOf(text) {
  const match = /\.(\d+)$/.exec(text);
  return match ? match[1].length : 0;
}

function numeric(raw, config) {
  if (typeof raw === "boolean") {
    return bad("type", "Enter a number.");
  }
  const text = String(raw).trim();
  if (!/^[-+]?(\d+(\.\d*)?|\.\d+)$/.test(text)) {
    return bad("type", "Enter a number.");
  }
  const decimals = config.decimals === undefined ? 2 : config.decimals;
  const trimmed = text.includes(".") ? text.replace(/0+$/, "").replace(/\.$/, "") : text;
  if (decimalsOf(trimmed) > decimals) {
    return bad("precision", decimals === 0 ? "Enter a whole number." : `Use at most ${decimals} decimal place${decimals === 1 ? "" : "s"}.`);
  }
  const n = Number(text);
  const unit = config.unit ? ` ${config.unit}` : "";
  if (config.min !== undefined && n < config.min) {
    return bad("out_of_range", `Value must be at least ${config.min}${unit}.`);
  }
  if (config.max !== undefined && n > config.max) {
    return bad("out_of_range", `Value must be at most ${config.max}${unit}.`);
  }
  return ok(n.toFixed(decimals));
}

function textValue(raw, config, multiline) {
  if (typeof raw !== "string") {
    return bad("type", "The answer must be text.");
  }
  let value = multiline ? raw.replace(/\r\n?/g, "\n") : raw;
  if (!multiline && /[\n\r]/.test(value)) {
    return bad("multiline", "This answer must be on a single line.");
  }
  if (CONTROL.test(value)) {
    return bad("characters", "The answer contains characters that are not allowed.");
  }
  value = value.trim();
  if (value.length < (config.min_length || 0)) {
    return bad("too_short", `Enter at least ${config.min_length} characters.`);
  }
  if (value.length > config.max_length) {
    return bad("too_long", `Enter at most ${config.max_length} characters.`);
  }
  return ok(value);
}

function optionValues(options) {
  return options.map((o) => o.value);
}

/** Normalize and validate ``raw`` for an item (``{answer_type, config, options}``).  Returns ``{ok, value, errors}``. */
export function normalize(item, raw, availableOptions) {
  const type = item.answer_type;
  const config = withDefaults(type, item.config);
  const options = availableOptions || optionValues(item.options || []);
  switch (type) {
    case "boolean": {
      if (typeof raw === "boolean") {
        return ok(raw);
      }
      const t = String(raw).trim().toLowerCase();
      return ["true", "1"].includes(t) ? ok(true) : ["false", "0"].includes(t) ? ok(false) : bad("invalid", "Answer must be yes or no.");
    }
    case "yes_no":
    case "yes_no_na": {
      const t = String(raw).trim().toLowerCase();
      const value = t === "n/a" ? "na" : t;
      const allowed = type === "yes_no" ? ["yes", "no"] : ["yes", "no", "na"];
      return allowed.includes(value) ? ok(value) : bad("invalid_choice", `Choose one of: ${allowed.join(", ")}.`);
    }
    case "single_choice":
      return typeof raw === "string" && options.includes(raw.trim()) ? ok(raw.trim()) : bad("invalid_option", "That option is not available for this question.");
    case "multiple_choice": {
      if (!Array.isArray(raw)) {
        return bad("type", "Provide a list of options.");
      }
      const chosen = [...new Set(raw)];
      if (!chosen.every((v) => typeof v === "string" && options.includes(v))) {
        return bad("invalid_option", "That option is not available for this question.");
      }
      const order = optionValues(item.options || []);
      chosen.sort((a, b) => order.indexOf(a) - order.indexOf(b));
      const low = config.min_select || 0;
      const high = config.max_select;
      if (chosen.length < low) {
        return bad("too_few", `Select at least ${low}.`);
      }
      if (high !== undefined && high !== null && chosen.length > high) {
        return bad("too_many", `Select at most ${high}.`);
      }
      return ok(chosen);
    }
    case "score": {
      const n = Number(raw);
      if (typeof raw === "boolean" || String(raw).trim() === "" || !Number.isInteger(n)) {
        return bad("type", "Score must be a whole number.");
      }
      if (n < config.min || n > config.max) {
        return bad("out_of_range", `Score must be between ${config.min} and ${config.max}.`);
      }
      return (n - config.min) % config.step ? bad("invalid_step", `Score must be in steps of ${config.step}.`) : ok(n);
    }
    case "numeric":
      return numeric(raw, config);
    case "text":
      return textValue(raw, config, false);
    case "long_text":
      return textValue(raw, config, true);
    case "date": {
      const t = String(raw).trim();
      const valid = /^\d{4}-\d{2}-\d{2}$/.test(t) && !Number.isNaN(Date.parse(`${t}T00:00:00Z`)) && new Date(`${t}T00:00:00Z`).toISOString().startsWith(t);
      return valid ? ok(t) : bad("invalid", "Enter a valid date.");
    }
    case "datetime": {
      const t = String(raw).trim();
      return /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}/.test(t) && !Number.isNaN(Date.parse(t)) ? ok(t) : bad("invalid", "Enter a valid date and time.");
    }
    case "matrix": {
      if (raw === null || typeof raw !== "object" || Array.isArray(raw)) {
        return bad("type", "Provide an answer for each row.");
      }
      const rows = new Set((config.rows || []).map((r) => r.key));
      const cols = new Set((config.columns || []).map((c) => c.key));
      const multiple = config.mode === "multiple";
      const out = {};
      for (const [row, cell] of Object.entries(raw)) {
        if (!rows.has(row)) {
          return bad("invalid_row", "That row does not exist in this matrix.");
        }
        if (cell === null || cell === "" || (Array.isArray(cell) && cell.length === 0)) {
          continue;
        }
        const picked = multiple ? cell : [cell];
        if (!Array.isArray(picked) || !picked.every((c) => cols.has(c))) {
          return bad("invalid_column", "That column does not exist in this matrix.");
        }
        out[row] = multiple ? picked : picked[0];
      }
      if (config.require_all_rows && [...rows].some((r) => !(r in out))) {
        return bad("incomplete", "Answer every row.");
      }
      return ok(out);
    }
    default:
      return bad("unsupported", "This question type cannot be answered offline.");
  }
}
