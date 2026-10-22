/* The offline workspace (entry point of /offline/app/).
 *
 * One small page - served by the service worker from a public, data-free shell - that reads everything from IndexedDB:
 *   #/            prepared checklists, what can be prepared, the sync panel
 *   #/a/<id>      a prepared checklist (answer, review, submit)
 *   #/prepare/<id>  prepare an assignment (arrives from "My work")
 *   #/conflicts   compare and resolve synchronization conflicts
 *   #/cache       manage what is stored on this device
 * It never claims server confirmation it does not have: answers say "saved on this device" until the server acknowledged them.
 */
import { openStorage, StorageFailure } from "./database.js";
import { Connectivity } from "./connectivity.js";
import { endSession, fetchSession, namespaceFor, recallAccount, rememberAccount } from "./account.js";
import { PackageCache, purgeOtherAccounts } from "./package-cache.js";
import { ExecutionStore } from "./execution-store.js";
import { SyncEngine } from "./sync-engine.js";
import { ConflictHandler } from "./conflict-handler.js";
import { queueSummary, LocalConflict } from "./sync-queue.js";
import { STORES, SYNC_STATE } from "./schema.js";
import { alert, answerControl, badge, confirmDialog, downloadJson, fmtTime, h } from "./offline-ui.js";
import { onSyncRequested, registerServiceWorker, requestBackgroundSync, warmShellCache } from "./service-worker-registration.js";

const root = document.getElementById("offline-app");
const RETENTION_KEY = "cp-offline-retention-days";
const app = { storage: null, ctx: null, account: null, verified: false, session: null };
let installPrompt = null;
const uiErrors = new Map(); // item code -> messages for the next render
let savedAt = null;
let autoSyncTimer = null;
let currentSection = null;

window.addEventListener("beforeinstallprompt", (event) => {
  event.preventDefault();
  installPrompt = event;
  render();
});

const retentionDays = () => Math.max(1, Number(localStorage.getItem(RETENTION_KEY)) || 14);

// ------------------------------------------------------------------------------------------------------------- boot
async function boot() {
  root.replaceChildren(h("p", { class: "text-sm text-muted-foreground", role: "status" }, "Opening offline storage…"));
  registerServiceWorker().then(() => warmShellCache());
  try {
    app.storage = await openStorage();
  } catch (error) {
    root.replaceChildren(alert("danger", error instanceof StorageFailure ? error.message : "Offline storage is not available in this browser."));
    return;
  }
  app.connectivity = new Connectivity().attach(window);
  const session = await fetchSession();
  app.session = session;
  if (session.status === "ok") {
    app.account = session.account;
    app.verified = true;
    rememberAccount(localStorage, session.account);
  } else if (session.status === "offline") {
    app.account = recallAccount(localStorage); // unverified: only the last signed-in account that has not signed out
    app.connectivity.report(false);
  } else {
    endSession(localStorage); // the server says nobody is signed in: nothing is opened
    app.account = null;
  }
  if (session.status === "no_organization") {
    app.account = null;
  }
  if (!app.account) {
    renderSignedOut(session.status);
    return;
  }
  const ns = namespaceFor(app.account);
  app.ctx = { storage: app.storage, ns, now: () => Date.now() };
  app.cache = new PackageCache({ ctx: app.ctx });
  app.store = new ExecutionStore({ ctx: app.ctx });
  const getSession = async () => {
    const s = await fetchSession();
    app.session = s;
    app.verified = s.status === "ok";
    if (s.status === "anonymous") {
      endSession(localStorage);
    }
    return s;
  };
  app.engine = new SyncEngine({ ctx: app.ctx, connectivity: app.connectivity, getSession, onChange: () => render() });
  app.conflicts = new ConflictHandler({ ctx: app.ctx, getSession, connectivity: app.connectivity });
  app.connectivity.subscribe(() => {
    render();
    if (app.connectivity.state === "online") {
      app.engine.run().then(render);
    }
  });
  onSyncRequested(() => app.engine.run().then(render));
  window.addEventListener("hashchange", render);
  await app.cache.purgeExpired(retentionDays());
  if (app.verified) {
    app.engine.start().then(render);
  }
  render();
}

function renderSignedOut(status) {
  const message = status === "offline" || status === "unavailable"
    ? "You are offline and no signed-in account is remembered on this device. Sign in while online to open your offline checklists."
    : status === "no_organization" ? "Choose an organization first, then open the offline workspace."
      : "Sign in to open your offline checklists. Nothing is shown until you do.";
  root.replaceChildren(h("div", { class: "card card-body max-w-xl" }, h("h2", { class: "card-title", text: "Offline checklists" }), h("p", { class: "mt-2 text-sm", text: message }),
    h("div", { class: "mt-4" }, h("a", { class: "btn btn-primary", href: "/auth/login/?next=/offline/app/" }, "Sign in"))));
}

// ------------------------------------------------------------------------------------------------------------- shell
function statusBar(summary, sync) {
  const online = app.connectivity.state === "online";
  const waiting = summary.pending + summary.sending + summary.failed;
  const engine = app.engine.status;
  const bits = [
    badge(online ? "Online" : app.connectivity.state === "offline" ? "Offline" : "Checking connection", online ? "success" : "warning"),
    waiting ? badge(`${waiting} change${waiting === 1 ? "" : "s"} waiting to synchronize`, "warning")
      : summary.conflicts || summary.rejected ? null : badge("Everything synchronized", "success"),
    summary.rejected ? badge(`${summary.rejected} change${summary.rejected === 1 ? "" : "s"} not accepted by the server`, "danger") : null,
    engine === "syncing" ? badge("Synchronizing…", "info") : null,
    engine === "auth" ? badge("Sign in to synchronize", "danger") : null,
    engine === "error" ? badge("Synchronization failed — will retry", "danger") : null,
    engine === "account_changed" ? badge("Different account signed in", "danger") : null,
    summary.conflicts ? h("a", { href: "#/conflicts" }, badge(`${summary.conflicts} conflict${summary.conflicts === 1 ? "" : "s"} need attention`, "danger")) : null,
    !app.verified ? badge("Identity not verified (offline)", "neutral") : null,
  ];
  return h("section", { class: "card card-body mb-4", "aria-label": "Synchronization status" },
    h("div", { class: "flex flex-wrap items-center gap-2" }, bits),
    h("div", { class: "mt-3 flex flex-wrap items-center gap-3 text-sm" },
      h("span", { class: "text-muted-foreground" }, `Last synchronized: ${sync && sync.lastSuccessAt ? fmtTime(sync.lastSuccessAt) : "never"}`),
      h("button", { type: "button", class: "btn btn-secondary btn-sm", disabled: engine === "syncing", onclick: async () => { await app.engine.run({ force: true }); render(); } }, "Sync now"),
      installPrompt ? h("button", { type: "button", class: "btn btn-ghost btn-sm", onclick: async () => { installPrompt.prompt(); installPrompt = null; render(); } }, "Install app") : null),
    app.engine.lastError ? h("p", { class: "mt-2 text-sm text-danger-text", role: "alert", text: app.engine.lastError }) : null);
}

function nav() {
  const link = (href, label) => h("a", { class: "btn btn-ghost btn-sm", href }, label);
  return h("nav", { class: "mb-4 flex flex-wrap gap-2", "aria-label": "Offline sections" },
    link("#/", "Checklists"), link("#/conflicts", "Conflicts"), link("#/cache", "Storage"), h("a", { class: "btn btn-ghost btn-sm", href: "/executions/" }, "Back to My work"));
}

async function render() {
  if (!app.ctx) {
    return;
  }
  const [summary, sync] = await Promise.all([queueSummary(app.ctx), app.storage.get(STORES.syncState, app.ctx.ns)]);
  const [, route = "", id = ""] = (location.hash || "#/").split("/");
  let body;
  try {
    body = route === "a" ? await renderWorkspace(id) : route === "prepare" ? await renderPrepare(id) : route === "conflicts" ? await renderConflicts()
      : route === "cache" ? await renderCache() : await renderHome();
  } catch (error) {
    body = alert("danger", error instanceof StorageFailure ? error.message : "Something went wrong while showing this page.");
  }
  root.replaceChildren(statusBar(summary, sync), nav(), body);
}

// ------------------------------------------------------------------------------------------------------------- home
async function fetchPreparable() {
  try {
    const response = await fetch("/api/v1/offline/assignments/?page_size=50", { credentials: "same-origin", cache: "no-store", headers: { Accept: "application/json" } });
    app.connectivity.report(true);
    if (!response.ok) return { error: "The list of assignments could not be loaded." };
    const body = await response.json();
    return { rows: body.data || [] };
  } catch (error) {
    app.connectivity.report(false);
    return { offline: true };
  }
}

async function renderHome() {
  const prepared = await app.cache.list();
  const ready = prepared.filter((p) => p.state === "ready");
  const sections = [h("section", { class: "card mb-4", "aria-labelledby": "prepared-h" },
    h("div", { class: "card-header" }, h("h2", { id: "prepared-h", class: "card-title", text: "Ready on this device" })),
    ready.length ? h("ul", { class: "divide-y divide-border" }, ready.map((p) => preparedRow(p)))
      : h("p", { class: "card-body text-sm text-muted-foreground", text: "Nothing is prepared yet. Prepare an assignment below while you are online." }))];
  const unfinished = prepared.filter((p) => p.state !== "ready");
  if (unfinished.length) {
    sections.push(alert("warning", "Some preparations did not finish and are not available offline: ", unfinished.map((p) => p.error || "interrupted").join(" · ")));
  }
  const listing = await fetchPreparable();
  const readyIds = new Set(ready.map((p) => p.assignmentId));
  const card = h("section", { class: "card", "aria-labelledby": "avail-h" }, h("div", { class: "card-header" }, h("h2", { id: "avail-h", class: "card-title", text: "Prepare for offline use" })));
  if (listing.offline) {
    card.append(h("p", { class: "card-body text-sm text-muted-foreground", text: "You are offline. Reconnect to prepare more checklists." }));
  } else if (listing.error) {
    card.append(h("p", { class: "card-body text-sm text-danger-text", text: listing.error }));
  } else if (!listing.rows.length) {
    card.append(h("p", { class: "card-body text-sm text-muted-foreground", text: "No open assignments to prepare." }));
  } else {
    card.append(h("ul", { class: "divide-y divide-border" }, listing.rows.map((a) => h("li", { class: "flex flex-wrap items-center justify-between gap-3 px-5 py-3 text-sm" },
      h("div", { class: "min-w-0" }, h("p", { class: "font-medium", text: `${a.checklist.name} · version ${a.checklist.version_number}` }),
        h("p", { class: "text-muted-foreground", text: `${a.location.name} · ${a.reference}${a.due_at ? ` · due ${fmtTime(Date.parse(a.due_at))}` : ""}` }),
        a.offline.capable ? null : h("p", { class: "text-warning-text", text: `Not available offline: ${a.offline.reasons.join("; ")}.` })),
      a.offline.capable
        ? (readyIds.has(a.id) ? badge("Ready", "success") : h("a", { class: "btn btn-primary btn-sm", href: `#/prepare/${a.id}` }, "Prepare for offline use"))
        : badge("Online only", "neutral")))));
  }
  sections.push(card);
  return h("div", {}, sections);
}

function preparedRow(p) {
  const pkg = p.package;
  return h("li", { class: "flex flex-wrap items-center justify-between gap-3 px-5 py-3 text-sm" },
    h("div", { class: "min-w-0" }, h("p", { class: "font-medium", text: `${pkg.checklist.name} · version ${pkg.checklist.version_number}` }),
      h("p", { class: "text-muted-foreground", text: `${pkg.assignment.location.path} · ${pkg.assignment.reference} · prepared ${fmtTime(p.preparedAt)}` })),
    h("div", { class: "flex flex-wrap items-center gap-2" },
      p.submitState === "submitted" || p.executionStatus === "submitted" ? badge("Submitted", "success") : p.submitState === "queued" ? badge("Submission waiting for the server", "warning")
        : p.blockedReason ? badge("Needs attention", "danger") : badge("Ready for offline use", "success"),
      p.unsynced ? badge(`${p.unsynced} not synchronized`, "warning") : null,
      h("a", { class: "btn btn-secondary btn-sm", href: `#/a/${p.assignmentId}` }, "Open")));
}

async function renderPrepare(assignmentId) {
  const log = h("ol", { class: "mt-3 space-y-1 text-sm", "aria-live": "polite" });
  const body = h("section", { class: "card card-body" }, h("h2", { class: "card-title", text: "Preparing for offline use" }), log);
  (async () => {
    const result = await app.cache.prepare(assignmentId, { onProgress: (p) => log.append(h("li", { text: p.label })) });
    if (result.status === "ready") {
      location.hash = `#/a/${assignmentId}`;
      return;
    }
    log.replaceChildren();
    body.append(alert(result.status === "kept" ? "warning" : "danger", result.message, result.reasons && result.reasons.length ? ` ${result.reasons.join("; ")}.` : ""),
      h("a", { class: "btn btn-secondary mt-3", href: "#/" }, "Back"));
  })();
  return body;
}

// ------------------------------------------------------------------------------------------------------------- workspace
function syncLabel(rec) {
  if (!rec) return null;
  const map = {
    [SYNC_STATE.pending]: ["Saved on this device · waiting to synchronize", "warning"], [SYNC_STATE.syncing]: ["Synchronizing…", "info"],
    [SYNC_STATE.synced]: ["Synchronized with the server", "success"], [SYNC_STATE.failed]: ["Not accepted by the server", "danger"], [SYNC_STATE.conflict]: ["Conflict — needs your decision", "danger"],
  };
  const [text, tone] = map[rec.syncState] || ["Unknown", "neutral"];
  return badge(text, tone);
}

async function renderWorkspace(assignmentId) {
  const state = await app.store.open(assignmentId);
  if (!state) {
    return alert("warning", "This checklist is not prepared on this device. ", h("a", { href: "#/" }, "Back to the list"));
  }
  const { pkg, evaluation, responses, progress } = state;
  const sectionCode = currentSection || pkg.sections[0].code;
  const visibleSections = pkg.sections.filter((s) => evaluation.sections[s.code].visible && pkg.items.some((i) => i.section === s.code && evaluation.items[i.code].visible));
  const current = visibleSections.find((s) => s.code === sectionCode) || visibleSections[0];
  const index = visibleSections.indexOf(current);
  const go = (s) => { currentSection = s.code; render(); };
  const header = h("section", { class: "mb-4" },
    h("h2", { class: "text-xl font-semibold", text: pkg.checklist.name }),
    h("p", { class: "text-sm text-muted-foreground", text: `${pkg.assignment.location.path} · version ${pkg.checklist.version_number} · ${pkg.assignment.reference}` }),
    pkg.assignment.instructions ? h("p", { class: "mt-2 text-sm whitespace-pre-line", text: pkg.assignment.instructions }) : null,
    h("p", { class: "mt-3 text-sm" }, h("span", { class: "font-medium", text: `${progress.percent}% complete` }), ` · ${progress.answered} of ${progress.total} ${progress.basis} questions`),
    h("progress", { class: "progress mt-1 w-full", value: progress.percent, max: 100, "aria-label": "Checklist progress" }),
    h("p", { role: "status", class: "mt-2 text-xs text-muted-foreground", text: savedAt ? `Saved on this device ${new Date(savedAt).toLocaleTimeString()} — not yet confirmed by the server unless marked synchronized.` : "Answers are saved on this device as you go." }));
  const notes = [];
  if (state.submitState === "submitted" || state.record.executionStatus === "submitted") notes.push(alert("success", "This checklist was submitted and accepted by the server. It is read-only."));
  else if (state.blockedReason === "execution_submitted") notes.push(alert("warning", "This checklist was already submitted from elsewhere and cannot be changed here. Your local answers are kept — see Conflicts."));
  else if (state.blockedReason) notes.push(alert("danger", "The server no longer accepts changes for this assignment. Your local answers are kept on this device — see Conflicts."));
  if (uiErrors.get("__submit")) notes.push(alert("danger", uiErrors.get("__submit").join(" ")));
  if (state.submitState === "queued") notes.push(alert("warning", "Submission is waiting for the server. The checklist is NOT submitted until the server accepts it."));
  if (state.submit && state.submit.state === "rejected") notes.push(alert("danger", "The server did not accept the submission: ", state.submit.error || "check the answers and try again."));
  const nav2 = h("nav", { class: "card mb-4", "aria-label": "Checklist sections" }, h("ol", { class: "flex flex-wrap gap-2 p-3" }, visibleSections.map((s) => {
    const items = pkg.items.filter((i) => i.section === s.code && evaluation.items[i.code].visible && evaluation.items[i.code].required);
    const done = items.filter((i) => ["answered", "not_applicable"].includes(evaluation.items[i.code].answer.status)).length;
    return h("li", {}, h("button", { type: "button", class: `btn btn-sm ${s === current ? "btn-primary" : "btn-secondary"}`, "aria-current": s === current ? "step" : null, onclick: () => go(s) }, `${s.name}${items.length ? ` (${done}/${items.length})` : ""}`));
  })));
  const items = current ? pkg.items.filter((i) => i.section === current.code && evaluation.items[i.code].visible) : [];
  const form = h("section", { class: "card" }, h("div", { class: "card-header" }, h("h3", { class: "card-title", text: current ? current.name : "No questions apply" })),
    h("div", { class: "card-body" }, items.map((item) => workspaceItem(assignmentId, item, state))),
    h("div", { class: "card-footer flex flex-wrap items-center justify-between gap-2" },
      h("div", { class: "flex gap-2" },
        index > 0 ? h("button", { type: "button", class: "btn btn-secondary", onclick: () => go(visibleSections[index - 1]) }, "Previous") : null,
        index >= 0 && index < visibleSections.length - 1 ? h("button", { type: "button", class: "btn btn-secondary", onclick: () => go(visibleSections[index + 1]) }, "Next") : null),
      state.readOnly || state.submitState === "queued" ? null : h("button", { type: "button", class: "btn btn-primary", onclick: () => submitFlow(assignmentId, state) }, "Review and submit")));
  return h("div", {}, header, notes, nav2, form);
}

function workspaceItem(assignmentId, item, state) {
  const result = state.evaluation.items[item.code];
  const rec = state.responses[item.code];
  const locked = state.readOnly || state.submitState === "queued" || (rec && rec.syncState === SYNC_STATE.conflict);
  const errors = uiErrors.get(item.code) || [];
  const save = async (patch) => {
    uiErrors.delete(item.code);
    try {
      const out = await app.store.saveAnswer(assignmentId, item.code, patch);
      if (!out.ok) {
        uiErrors.set(item.code, out.errors.map((e) => e.message));
      } else {
        savedAt = out.savedAt;
        requestBackgroundSync();
        clearTimeout(autoSyncTimer);
        autoSyncTimer = setTimeout(() => app.connectivity.state !== "offline" && app.engine.run().then(render), 1500); // event-driven, not polling
      }
    } catch (error) {
      uiErrors.set(item.code, [error instanceof StorageFailure || error instanceof LocalConflict ? error.message : "This answer could not be saved on this device."]);
    }
    render();
  };
  const current = rec && rec.status === "answered" ? rec.value : null;
  const comment = rec ? rec.comment : "";
  const control = answerControl(item, result, current, { disabled: locked, onValue: (raw) => save({ raw, comment }) });
  const na = !result.required && !["photo", "file", "signature"].includes(item.answer_type)
    ? h("label", { class: "mt-2 flex items-center gap-2 text-sm" }, h("input", { type: "checkbox", class: "checkbox", checked: !!(rec && rec.status === "not_applicable"), disabled: locked,
      onchange: (e) => save({ raw: null, notApplicable: e.target.checked, comment }) }), "Not applicable") : null;
  const commentBox = h("details", { class: "mt-2", open: !!comment || result.commentRequired },
    h("summary", { class: "cursor-pointer text-sm text-muted-foreground", text: result.commentRequired ? "Comment (required)" : "Add a comment" }),
    result.commentPrompts.map((p) => h("p", { class: "mt-1 text-sm", text: p })),
    h("textarea", { class: "textarea mt-1", rows: 2, maxlength: 2000, "aria-label": `Comment for: ${item.question}`, disabled: locked || !rec, onchange: (e) => save({ raw: rec.status === "answered" ? rec.value : null, notApplicable: rec.status === "not_applicable", comment: e.target.value }) }));
  commentBox.querySelector("textarea").value = comment;
  if (!rec) commentBox.querySelector("textarea").setAttribute("placeholder", "Answer the question first, then add a comment.");
  return h("div", { class: "border-b border-border py-5 last:border-b-0" },
    h("p", { class: "label" }, item.question, result.required ? h("span", { class: "ml-1 text-xs font-normal text-danger-text", text: "(required)" }) : null),
    item.help_text || item.instructions ? h("p", { class: "mb-2 text-sm text-muted-foreground", text: [item.help_text, item.instructions].filter(Boolean).join(" · ") }) : null,
    result.instructions.map((t) => h("p", { class: "mb-2 text-sm", text: t })), result.messages.map((m) => h("p", { class: "mb-2 text-sm", text: m.message })),
    control, na, item.answer_type === "photo" ? null : commentBox,
    h("p", { class: "mt-2 text-xs" }, syncLabel(rec), rec && rec.error ? h("span", { class: "ml-2 text-danger-text", text: rec.error }) : null),
    errors.map((m) => h("p", { class: "mt-2 text-sm text-danger-text", role: "alert", text: m })));
}

async function submitFlow(assignmentId, state) {
  const first = state.problems;
  if (Object.keys(first).length) {
    const lines = Object.entries(first).map(([code, list]) => `${state.pkg.items.find((i) => i.code === code).question} — ${list.join(" ")}`);
    await confirmDialog({ title: "Fix these before submitting", message: lines.join("\n"), confirmLabel: "OK" });
    return;
  }
  const online = app.connectivity.state === "online";
  const ok = await confirmDialog({
    title: "Submit this checklist?",
    message: online ? "Your answers will be sent to the server, which checks them again. The checklist is only submitted once the server accepts it."
      : "You are offline. The submission is saved on this device and sent when you reconnect. It is NOT submitted until the server accepts it.",
    confirmLabel: "Submit",
  });
  if (!ok) return;
  try {
    const out = await app.store.submit(assignmentId);
    if (!out.ok) {
      uiErrors.set("__submit", Object.values(out.problems).flat());
    }
  } catch (error) {
    uiErrors.set("__submit", [error.message]);
  }
  if (app.connectivity.state !== "offline") await app.engine.run();
  render();
}

// ------------------------------------------------------------------------------------------------------------- conflicts
async function renderConflicts() {
  const list = await app.conflicts.list();
  if (!list.length) {
    return h("section", { class: "card card-body" }, h("p", { class: "text-sm", text: "No conflicts. Everything on this device is either synchronized or waiting to be." }));
  }
  const out = [];
  for (const c of list) {
    const rec = await app.storage.get(STORES.packages, `${app.ctx.ns}|${c.assignmentId}`);
    const item = rec ? rec.package.items.find((i) => i.id === c.itemId) : null;
    const show = (v) => (v === null || v === undefined ? "(empty)" : typeof v === "object" ? JSON.stringify(v) : String(v));
    const status = h("p", { class: "mt-2 text-sm", role: "status" });
    const run = async (task) => {
      const result = await task();
      if (result.status === "applied") { render(); return; }
      status.textContent = result.status === "conflict" ? "The server changed again. Compare the new values." : (result.message || (result.errors && result.errors[0] && result.errors[0].message) || "The server did not accept this.");
      if (result.status === "conflict") render();
    };
    const resolvable = app.conflicts.resolvable(c);
    out.push(h("section", { class: "card mb-4", "aria-label": "Conflict" },
      h("div", { class: "card-header" }, h("h3", { class: "card-title", text: item ? item.question : "Answer" }), badge(c.code.replaceAll("_", " "), "danger")),
      h("div", { class: "card-body" }, h("p", { class: "text-sm", text: c.message }),
        c.type === "submit" ? null : h("table", { class: "table mt-3" }, h("thead", {}, h("tr", {}, h("th", { scope: "col" }, "Version"), h("th", { scope: "col" }, "Answer"), h("th", { scope: "col" }, "Comment"))),
          h("tbody", {}, h("tr", {}, h("th", { scope: "row" }, "Mine (on this device)"), h("td", { text: show(c.local && c.local.value) }), h("td", { text: show(c.local && c.local.comment) })),
            h("tr", {}, h("th", { scope: "row" }, "Server"), h("td", { text: c.server && c.server.exists ? show(c.server.value) : "(no answer)" }), h("td", { text: show(c.server && c.server.comment) })))),
        resolvable ? h("div", { class: "mt-4 flex flex-wrap gap-2" },
          h("button", { type: "button", class: "btn btn-secondary", onclick: () => run(() => app.conflicts.keepServer(c)) }, "Keep the server's answer"),
          h("button", { type: "button", class: "btn btn-primary", onclick: () => run(() => app.conflicts.useLocal(c)) }, "Use my answer")) : h("p", { class: "mt-3 text-sm", text: "The server will not reopen this checklist, so this cannot be resolved here. Your answer is kept on this device." }),
        h("div", { class: "mt-3 flex flex-wrap gap-2" },
          h("button", { type: "button", class: "btn btn-ghost btn-sm", onclick: async () => downloadJson(`offline-answers-${c.assignmentId}.json`, await app.conflicts.recoveryExport(c.assignmentId)) }, "Download my answers"),
          !resolvable ? h("button", { type: "button", class: "btn btn-danger btn-sm", onclick: async () => {
            if (await confirmDialog({ title: "Discard your local answer?", message: "It cannot be recovered afterwards. Download your answers first if you may need them.", confirmLabel: "Discard", danger: true })) { await app.conflicts.discard(c); render(); }
          } }, "Discard my answer") : null),
        status)));
  }
  return h("div", {}, out);
}

// ------------------------------------------------------------------------------------------------------------- storage
async function renderCache() {
  const rows = await app.cache.list();
  const table = h("table", { class: "table" }, h("thead", {}, h("tr", {}, ["Checklist", "State", "Prepared", "Not synchronized", "Actions"].map((t) => h("th", { scope: "col", text: t })))),
    h("tbody", {}, rows.map((p) => h("tr", {},
      h("td", { text: p.package ? `${p.package.checklist.name} v${p.package.checklist.version_number} · ${p.package.assignment.reference}` : "(unfinished preparation)" }),
      h("td", {}, badge(p.state === "ready" ? "Ready" : p.state === "failed" ? "Failed" : "Not ready", p.state === "ready" ? "success" : "warning")),
      h("td", { text: fmtTime(p.preparedAt || p.startedAt) }), h("td", { text: String(p.unsynced || 0) }),
      h("td", {}, h("button", { type: "button", class: "btn btn-secondary btn-sm", onclick: async () => removeFlow(p) }, "Remove from this device"))))));
  const retention = h("input", { class: "input max-w-[8rem]", type: "number", min: 1, max: 365, value: retentionDays(), "aria-label": "Days to keep synchronized copies",
    onchange: (e) => { localStorage.setItem(RETENTION_KEY, String(Math.max(1, Number(e.target.value) || 14))); } });
  return h("div", {},
    h("section", { class: "card mb-4" }, h("div", { class: "card-header" }, h("h2", { class: "card-title", text: "Stored on this device" })),
      rows.length ? h("div", { class: "overflow-x-auto" }, table) : h("p", { class: "card-body text-sm text-muted-foreground", text: "Nothing is stored." })),
    h("section", { class: "card card-body mb-4" }, h("h3", { class: "card-title", text: "Retention" }),
      h("p", { class: "mt-2 text-sm text-muted-foreground", text: "Fully synchronized copies are removed automatically after this many days. Changes that are not synchronized are never removed automatically." }),
      h("label", { class: "label mt-3" }, "Days to keep synchronized copies"), retention),
    h("section", { class: "card card-body" }, h("h3", { class: "card-title", text: "Privacy" }),
      h("p", { class: "mt-2 text-sm text-muted-foreground", text: "Offline data is stored in this browser profile and is not encrypted by this application; anyone who can use this profile can read it. On a shared device, remove checklists when you are done and sign out. No password or sign-in token is ever stored." }),
      h("button", { type: "button", class: "btn btn-secondary mt-3", onclick: async () => {
        const r = await purgeOtherAccounts(app.storage, app.ctx.ns);
        alertStatus(`Removed ${r.removed} synchronized copy(ies) of other accounts${r.kept ? `; ${r.kept} with unsynchronized work were kept for their owner.` : "."}`);
      } }, "Clear synchronized data of other accounts"), h("p", { id: "cache-status", role: "status", class: "mt-2 text-sm" })));
}

function alertStatus(text) {
  const el = document.getElementById("cache-status");
  if (el) el.textContent = text;
}

async function removeFlow(p) {
  if (!p.unsynced) {
    await app.cache.remove(p.assignmentId, { discardUnsynced: true });
    render();
    return;
  }
  const ok = await confirmDialog({
    title: "This copy has unsynchronized work", confirmLabel: "Remove and discard my changes", danger: true,
    message: `${p.unsynced} change(s) have not reached the server. Removing this copy permanently discards them. Synchronize first, or download your answers.`,
    extra: h("button", { type: "button", class: "btn btn-secondary btn-sm mt-3", onclick: async () => downloadJson(`offline-answers-${p.assignmentId}.json`, await app.conflicts.recoveryExport(p.assignmentId)) }, "Download my answers"),
  });
  if (ok) {
    await app.cache.remove(p.assignmentId, { discardUnsynced: true });
    render();
  }
}

// Sign-out hook: the namespace stays on disk, but the remembered account stops opening it.
document.addEventListener("submit", (event) => {
  const action = event.target && event.target.getAttribute && event.target.getAttribute("action");
  if (action && action.includes("/auth/logout")) {
    endSession(localStorage);
  }
});

boot();

