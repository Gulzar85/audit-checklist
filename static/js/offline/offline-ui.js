/* DOM building blocks of the offline workspace.  Everything user- or server-supplied is written with textContent / setAttribute - never
 * innerHTML - so a stored answer or checklist text cannot inject markup (the page also runs under a strict CSP).
 */

export function h(tag, attrs = {}, ...children) {
  const el = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs || {})) {
    if (value === null || value === undefined || value === false) {
      continue;
    }
    if (key === "class") {
      el.className = value;
    } else if (key === "text") {
      el.textContent = value;
    } else if (key.startsWith("on") && typeof value === "function") {
      el.addEventListener(key.slice(2).toLowerCase(), value);
    } else if (value === true) {
      el.setAttribute(key, "");
    } else {
      el.setAttribute(key, String(value));
    }
  }
  for (const child of children.flat()) {
    if (child === null || child === undefined || child === false) {
      continue;
    }
    el.append(child.nodeType ? child : document.createTextNode(String(child)));
  }
  return el;
}

export const badge = (text, tone = "neutral") => h("span", { class: `badge badge-${tone}` }, text);

export function alert(tone, ...children) {
  return h("div", { class: `alert alert-${tone}`, role: tone === "danger" ? "alert" : "status" }, h("div", {}, ...children));
}

export function fmtTime(ms) {
  return ms ? new Date(ms).toLocaleString([], { dateStyle: "medium", timeStyle: "short" }) : "never";
}

/** A modal confirmation (native <dialog>).  Resolves true only on the confirm button. */
export function confirmDialog({ title, message, confirmLabel = "Confirm", danger = false, extra = null }) {
  return new Promise((resolve) => {
    const dialog = h("dialog", { class: "modal-dialog", "aria-labelledby": "dlg-title" });
    const done = (value) => {
      dialog.close();
      dialog.remove();
      resolve(value);
    };
    dialog.append(h("div", { class: "p-6" },
      h("h2", { id: "dlg-title", class: "text-lg font-semibold", text: title }),
      h("p", { class: "mt-2 text-sm text-muted-foreground", text: message }),
      extra,
      h("div", { class: "mt-6 flex justify-end gap-2" },
        h("button", { type: "button", class: "btn btn-secondary", onclick: () => done(false) }, "Cancel"),
        h("button", { type: "button", class: `btn ${danger ? "btn-danger" : "btn-primary"}`, onclick: () => done(true) }, confirmLabel))));
    dialog.addEventListener("cancel", () => done(false));
    document.body.append(dialog);
    dialog.showModal();
  });
}

export function downloadJson(filename, data) {
  const blob = new Blob([JSON.stringify(data, null, 2)], { type: "application/json" });
  const url = URL.createObjectURL(blob);
  const a = h("a", { href: url, download: filename });
  document.body.append(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

/** One answer control for an item (same shapes as the answer contract).  ``onValue(raw)`` is called when the user commits a change. */
export function answerControl(item, evalResult, current, { onValue, disabled }) {
  const id = `f-${item.id}`;
  const opts = (item.options || []).filter((o) => !evalResult.options || evalResult.options.available.includes(o.value));
  const type = item.answer_type;
  const common = { id, disabled };
  if (type === "yes_no" || type === "yes_no_na" || type === "boolean") {
    const choices = type === "boolean" ? [["true", "Yes"], ["false", "No"]] : [["yes", "Yes"], ["no", "No"], ...(type === "yes_no_na" ? [["na", "N/A"]] : [])];
    const now = current === true ? "true" : current === false ? "false" : current;
    return h("div", { class: "flex flex-wrap gap-4", role: "radiogroup", "aria-label": item.question },
      choices.map(([value, label]) => h("label", { class: "flex items-center gap-2 text-sm" },
        h("input", { type: "radio", class: "radio", name: `r-${item.id}`, value, checked: now === value, disabled, onchange: () => onValue(value) }), label)));
  }
  if (type === "single_choice") {
    if (item.config.display === "dropdown") {
      return h("select", { ...common, class: "select", onchange: (e) => onValue(e.target.value) },
        h("option", { value: "" }, "Choose…"), opts.map((o) => h("option", { value: o.value, selected: current === o.value }, o.label)));
    }
    return h("div", { class: "flex flex-wrap gap-4", role: "radiogroup", "aria-label": item.question },
      opts.map((o) => h("label", { class: "flex items-center gap-2 text-sm" },
        h("input", { type: "radio", class: "radio", name: `r-${item.id}`, value: o.value, checked: current === o.value, disabled, onchange: () => onValue(o.value) }), o.label)));
  }
  if (type === "multiple_choice") {
    const chosen = new Set(Array.isArray(current) ? current : []);
    const wrap = h("div", { class: "flex flex-wrap gap-4", role: "group", "aria-label": item.question });
    const collect = () => [...wrap.querySelectorAll("input:checked")].map((i) => i.value);
    opts.forEach((o) => wrap.append(h("label", { class: "flex items-center gap-2 text-sm" },
      h("input", { type: "checkbox", class: "checkbox", value: o.value, checked: chosen.has(o.value), disabled, onchange: () => onValue(collect()) }), o.label)));
    return wrap;
  }
  if (type === "score") {
    const { min, max, step } = { min: 1, max: 5, step: 1, ...item.config };
    const values = [];
    for (let v = min; v <= max; v += step) values.push(v);
    return h("div", { class: "flex flex-wrap gap-3", role: "radiogroup", "aria-label": item.question },
      values.map((v) => h("label", { class: "flex items-center gap-1 text-sm" },
        h("input", { type: "radio", class: "radio", name: `r-${item.id}`, value: String(v), checked: Number(current) === v && current !== null && current !== undefined, disabled, onchange: () => onValue(String(v)) }), String(v))));
  }
  if (type === "numeric") {
    const decimals = item.config.decimals === undefined ? 2 : item.config.decimals;
    return h("div", { class: "flex items-center gap-2" },
      h("input", { ...common, class: "input max-w-xs", type: "number", inputmode: "decimal", step: decimals === 0 ? "1" : `0.${"0".repeat(decimals - 1)}1`, min: item.config.min, max: item.config.max,
        value: current ?? "", onchange: (e) => onValue(e.target.value) }),
      item.config.unit ? h("span", { class: "text-sm text-muted-foreground", text: item.config.unit }) : null);
  }
  if (type === "text") {
    return h("input", { ...common, class: "input", type: "text", maxlength: item.config.max_length || 255, value: current ?? "", onchange: (e) => onValue(e.target.value) });
  }
  if (type === "long_text") {
    const area = h("textarea", { ...common, class: "textarea", rows: 3, maxlength: item.config.max_length || 5000, onchange: (e) => onValue(e.target.value) });
    area.value = current ?? "";
    return area;
  }
  if (type === "date" || type === "datetime") {
    const value = type === "datetime" && typeof current === "string" ? current.slice(0, 16) : current ?? "";
    return h("input", { ...common, class: "input max-w-xs", type: type === "date" ? "date" : "datetime-local", value, onchange: (e) => onValue(e.target.value) });
  }
  if (type === "matrix") {
    const multiple = item.config.mode === "multiple";
    const cur = current && typeof current === "object" ? current : {};
    const table = h("table", { class: "table" });
    const collect = () => {
      const out = {};
      for (const row of item.config.rows) {
        const picked = [...table.querySelectorAll(`input[data-row="${row.key}"]:checked`)].map((i) => i.value);
        if (picked.length) out[row.key] = multiple ? picked : picked[0];
      }
      return out;
    };
    table.append(h("thead", {}, h("tr", {}, h("th", { scope: "col" }, h("span", { class: "sr-only" }, "Row")), item.config.columns.map((c) => h("th", { scope: "col", text: c.label })))));
    table.append(h("tbody", {}, item.config.rows.map((row) => h("tr", {}, h("th", { scope: "row", text: row.label }),
      item.config.columns.map((c) => h("td", {}, h("input", {
        type: multiple ? "checkbox" : "radio", class: multiple ? "checkbox" : "radio", name: `m-${item.id}-${row.key}`, "data-row": row.key, value: c.key,
        checked: multiple ? (cur[row.key] || []).includes(c.key) : cur[row.key] === c.key, disabled, "aria-label": `${row.label}: ${c.label}`, onchange: () => onValue(collect()),
      })))))));
    return h("div", { class: "overflow-x-auto" }, table);
  }
  return alert("warning", "This question type cannot be answered offline.");
}
