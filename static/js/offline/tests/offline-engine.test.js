import { test } from "node:test";
import assert from "node:assert/strict";
import { MemoryStorage } from "../memory-store.js";
import { StorageFailure } from "../database.js";
import { Connectivity } from "../connectivity.js";
import { PackageCache, purgeOtherAccounts, unsyncedCount } from "../package-cache.js";
import { ExecutionStore } from "../execution-store.js";
import { SyncEngine, toWire } from "../sync-engine.js";
import { ConflictHandler } from "../conflict-handler.js";
import { OP_STATE, STORES, SYNC_STATE, validatePackage } from "../schema.js";
import { MAX_ATTEMPTS, queueSummary, recoverInterrupted } from "../sync-queue.js";
import { fetchSession, recallAccount, rememberAccount, endSession } from "../account.js";
import { A, ACCOUNT, FakeServer, ITEMS, NS, ORG, fixturePackage } from "./fake-server.js";

function setup({ server = new FakeServer(), ns = NS, storage = new MemoryStorage(), clock = { t: 1_000_000 } } = {}) {
  const ctx = { storage, ns, now: () => clock.t };
  const connectivity = new Connectivity({ fetchImpl: server.fetch });
  const getSession = async () => fetchSession(server.fetch);
  const cache = new PackageCache({ ctx, fetchImpl: server.fetch });
  const store = new ExecutionStore({ ctx });
  const engine = new SyncEngine({ ctx, connectivity, getSession, fetchImpl: server.fetch, locks: null, setTimer: () => 0, clearTimer: () => {} });
  const conflicts = new ConflictHandler({ ctx, getSession, fetchImpl: server.fetch, connectivity });
  return { server, ctx, storage, clock, cache, store, engine, conflicts, connectivity };
}

async function prepared(opts) {
  const s = setup(opts);
  const r = await s.cache.prepare(A);
  assert.equal(r.status, "ready");
  return s;
}

// ------------------------------------------------------------------------------------------------------ preparation
test("preparation stores a verified, ready package and seeds the saved server answers", async () => {
  const server = new FakeServer(fixturePackage({ execution: { id: "e".repeat(8) + "-0000-4000-8000-000000000000", status: "in_progress", revision: 2, responses: [{ item: ITEMS.exit, code: "EXIT", status: "answered", value: "yes", comment: "", revision: 3 }] } }));
  const s = await prepared({ server });
  const opened = await s.store.open(A);
  assert.equal(opened.record.state, "ready");
  assert.equal(opened.responses.EXIT.syncState, SYNC_STATE.synced);
  assert.equal(opened.responses.EXIT.baseRevision, 3);
  assert.equal(await unsyncedCount(s.ctx, A), 0);
});

test("an offline download is not ready", async () => {
  const s = setup();
  s.server.down = true;
  const r = await s.cache.prepare(A);
  assert.equal(r.status, "failed");
  assert.equal(await s.cache.get(A), null);
  assert.equal(await s.store.open(A), null);
  const rows = await s.cache.list();
  assert.deepEqual(rows.map((x) => x.state), ["failed"]);
});

test("an invalid or partial package never becomes ready", async () => {
  for (const bad of [{ items: [] }, { sections: [] }, { package_hash: "nope" }, { schema_version: 99 }, { evaluator_version: 3 }]) {
    const s = setup({ server: new FakeServer(fixturePackage(bad)) });
    const r = await s.cache.prepare(A);
    assert.equal(r.status, "failed", JSON.stringify(Object.keys(bad)));
    assert.equal(await s.cache.get(A), null);
  }
  assert.throws(() => validatePackage(fixturePackage({ execution: { id: "e".repeat(8) + "-0000-4000-8000-000000000000", status: "in_progress", responses: [{ item: "44444444-4444-4444-8444-444444444444" }] } })), /unknown question/);
});

test("a storage failure while promoting leaves the assignment not ready", async () => {
  const s = setup();
  const original = s.storage.batch.bind(s.storage);
  s.storage.batch = async (ops) => {
    if (ops.some((o) => o.store === STORES.packages && o.record && o.record.state === "ready")) {
      throw new StorageFailure("quota", "no space");
    }
    return original(ops);
  };
  const r = await s.cache.prepare(A);
  assert.equal(r.status, "failed");
  assert.match(r.message, /space/);
  assert.equal(await s.cache.get(A), null);
});

test("preparing again never replaces a copy that has unsynchronized work", async () => {
  const s = await prepared();
  await s.store.saveAnswer(A, "EXIT", { raw: "yes" });
  const again = await s.cache.prepare(A);
  assert.equal(again.status, "kept");
  assert.equal((await s.store.open(A)).responses.EXIT.value, "yes");
});

test("a different checklist version is never swapped in silently", async () => {
  const s = await prepared();
  s.server.pkg = fixturePackage({ checklist: { ...fixturePackage().checklist, version_id: "99999999-9999-4999-8999-999999999999" }, package_hash: "b".repeat(64) });
  const r = await s.cache.prepare(A);
  assert.equal(r.status, "failed");
  assert.match(r.message, /different checklist version/);
  assert.equal((await s.cache.get(A)).versionId, fixturePackage().checklist.version_id);
});

// ------------------------------------------------------------------------------------------------------ local save
test("answers are saved locally, restored on reload and shown as pending - not as synced", async () => {
  const s = await prepared();
  const r = await s.store.saveAnswer(A, "EXIT", { raw: "yes" });
  assert.equal(r.ok, true);
  const reopened = await new ExecutionStore({ ctx: s.ctx }).open(A); // a "reload": a fresh store over the same database
  assert.equal(reopened.responses.EXIT.value, "yes");
  assert.equal(reopened.responses.EXIT.syncState, SYNC_STATE.pending);
  assert.deepEqual(reopened.counts, { local: 0, pending: 1, syncing: 0, synced: 0, failed: 0, conflict: 0 });
  assert.equal(reopened.progress.answered, 1);
});

test("'saved' is only reported after the write committed: a failed write throws and stores nothing", async () => {
  const s = await prepared();
  s.storage.failNextWrite = new StorageFailure("quota", "full");
  await assert.rejects(s.store.saveAnswer(A, "EXIT", { raw: "yes" }), /full/);
  assert.equal((await s.store.open(A)).responses.EXIT, undefined);
  assert.equal((await queueSummary(s.ctx)).total, 0);
});

test("invalid input is refused locally and never queued", async () => {
  const s = await prepared();
  const r = await s.store.saveAnswer(A, "TEMP", { raw: "99" });
  assert.equal(r.ok, false);
  assert.equal(r.errors[0].code, "out_of_range");
  assert.equal((await queueSummary(s.ctx)).total, 0);
  const na = await s.store.saveAnswer(A, "EXIT", { raw: null, notApplicable: true });
  assert.equal(na.ok, false); // a required question cannot be N/A
});

test("repeated edits of one unsent answer are coalesced into one operation with a stable id", async () => {
  const s = await prepared();
  await s.store.saveAnswer(A, "TEMP", { raw: "1" });
  const first = (await s.storage.getAll(STORES.operations))[0].opId;
  for (const v of ["2", "3", "4.5"]) await s.store.saveAnswer(A, "TEMP", { raw: v });
  const ops = await s.storage.getAll(STORES.operations);
  assert.equal(ops.length, 1);
  assert.equal(ops[0].opId, first);
  assert.equal(ops[0].payload.value, "4.5");
  assert.equal(ops[0].baseRevision, 0);
});

test("clearing an answer the server has never seen removes it without a round trip", async () => {
  const s = await prepared();
  await s.store.saveAnswer(A, "TEMP", { raw: "3" });
  await s.store.saveAnswer(A, "TEMP", { raw: "" });
  assert.equal((await queueSummary(s.ctx)).total, 0);
  assert.equal((await s.store.open(A)).responses.TEMP, undefined);
});

test("rapid concurrent writes are serialized: the last one wins locally and in the queue", async () => {
  const s = await prepared();
  await Promise.all(["1", "2", "3", "4", "5"].map((v) => s.store.saveAnswer(A, "TEMP", { raw: v })));
  const opened = await s.store.open(A);
  assert.equal(opened.responses.TEMP.value, "5.0");
  assert.equal(opened.responses.TEMP.localRevision, 5);
  assert.equal((await s.storage.getAll(STORES.operations)).length, 1);
});

test("a hidden follow-up cannot be answered; the rule reveals and requires it locally", async () => {
  const s = await prepared();
  await assert.rejects(s.store.saveAnswer(A, "WHY", { raw: "x" }), /does not apply/);
  await s.store.saveAnswer(A, "EXIT", { raw: "no" });
  const opened = await s.store.open(A);
  assert.equal(opened.evaluation.items.WHY.visible, true);
  assert.equal(opened.evaluation.items.WHY.required, true);
  assert.equal(opened.progress.total, 3);
});

// ------------------------------------------------------------------------------------------------------ synchronization
test("sync applies the queue, then confirms locally only after the server's answer", async () => {
  const s = await prepared();
  await s.store.saveAnswer(A, "EXIT", { raw: "yes" });
  await s.store.saveAnswer(A, "TEMP", { raw: "4.5" });
  const result = await s.engine.run();
  assert.equal(result.ok, true);
  assert.equal(result.applied, 2);
  const opened = await s.store.open(A);
  assert.equal(opened.responses.EXIT.syncState, SYNC_STATE.synced);
  assert.equal(opened.responses.EXIT.baseRevision, 1);
  assert.equal((await queueSummary(s.ctx)).total, 0);
  assert.equal(s.server.rows.get(ITEMS.temp).value, "4.5");
  assert.equal(s.server.requests.at(-1).init.headers["X-CSRFToken"], "tok");
});

test("a lost response is recovered by retrying the same operation ids - nothing is applied twice", async () => {
  const s = await prepared();
  await s.store.saveAnswer(A, "TEMP", { raw: "4" });
  const opId = (await s.storage.getAll(STORES.operations))[0].opId;
  s.server.dropResponse = true;
  const lost = await s.engine.run();
  assert.equal(lost.ok, false);
  assert.equal(s.server.rows.get(ITEMS.temp).revision, 1); // the server did apply it
  let op = (await s.storage.getAll(STORES.operations))[0];
  assert.equal(op.opId, opId);
  assert.equal(op.state, OP_STATE.pending); // still queued: sent is not confirmed
  assert.equal((await s.store.open(A)).responses.TEMP.syncState, SYNC_STATE.pending);
  s.server.dropResponse = false;
  s.clock.t += 10 * 60 * 1000;
  const retried = await s.engine.run();
  assert.equal(retried.ok, true);
  assert.equal(s.server.rows.get(ITEMS.temp).revision, 1); // still one application
  assert.equal((await s.store.open(A)).responses.TEMP.syncState, SYNC_STATE.synced);
  assert.equal(s.server.requests.filter((r) => r.url.endsWith("/sync/")).length, 2);
});

test("transient failures back off, are bounded, and are reset by Sync now", async () => {
  const s = await prepared();
  await s.store.saveAnswer(A, "TEMP", { raw: "4" });
  s.server.status = 503; // reachable but failing: each attempt counts against the retry budget
  for (let i = 0; i < MAX_ATTEMPTS; i += 1) {
    s.clock.t += 10 * 60 * 1000;
    await s.engine.run();
  }
  let summary = await queueSummary(s.ctx);
  assert.equal(summary.failed, 1);
  s.server.status = null;
  s.clock.t += 60_000;
  assert.equal((await s.engine.run()).applied, 0); // failed entries are not retried on their own
  const forced = await s.engine.run({ force: true });
  assert.equal(forced.applied, 1);
  summary = await queueSummary(s.ctx);
  assert.equal(summary.total, 0);
});

test("only one sync loop runs at a time", async () => {
  const s = await prepared();
  await s.store.saveAnswer(A, "TEMP", { raw: "4" });
  const [a, b] = [s.engine.run(), s.engine.run()];
  assert.strictEqual(a, b);
  await a;
  assert.equal(s.server.requests.filter((r) => r.url.endsWith("/sync/")).length, 1);
});

test("an edit made while an operation is in flight chains behind it and cannot be overwritten", async () => {
  const s = await prepared();
  await s.store.saveAnswer(A, "TEMP", { raw: "4" });
  let release;
  const gate = new Promise((r) => { release = r; });
  const real = s.server.fetch;
  s.engine.fetchImpl = async (url, init) => {
    if (url.endsWith("/sync/")) await gate;
    return real(url, init);
  };
  const running = s.engine.run();
  await new Promise((r) => setTimeout(r, 20));
  await s.store.saveAnswer(A, "TEMP", { raw: "7" }); // typed while the first is on the wire
  const ops = await s.storage.getAll(STORES.operations);
  assert.equal(ops.length, 2);
  const chained = ops.find((o) => o.dependsOn);
  assert.equal(chained.baseRevision, null);
  release();
  const finished = await running;
  assert.equal(finished.applied, 2); // the first, then the chained one (its base revision came from the first acknowledgement)
  assert.equal((await s.engine.run()).sent, 0);
  assert.equal(s.server.rows.get(ITEMS.temp).value, "7.0");
  assert.equal(s.server.rows.get(ITEMS.temp).revision, 2);
  assert.equal((await s.store.open(A)).responses.TEMP.syncState, SYNC_STATE.synced);
});

test("a server validation failure keeps the local answer, marks it failed and is replaced by a new edit", async () => {
  const s = await prepared();
  s.server.pkg.items[1].config.max = 100; // the client allows 99; the server's rule says 10
  await s.cache.remove(A);
  await s.cache.prepare(A);
  await s.store.saveAnswer(A, "TEMP", { raw: "99" });
  const r = await s.engine.run();
  assert.equal(r.rejected, 1);
  const opened = await s.store.open(A);
  assert.equal(opened.responses.TEMP.value, "99.0");
  assert.equal(opened.responses.TEMP.syncState, SYNC_STATE.failed);
  assert.match(opened.responses.TEMP.error, /too high/);
  await s.store.saveAnswer(A, "TEMP", { raw: "5" });
  assert.equal((await s.engine.run()).applied, 1);
  assert.equal((await s.storage.getAll(STORES.operations)).length, 0);
});

test("submit is queued, not 'submitted', until the server accepts it - and waits for answers that failed", async () => {
  const s = await prepared();
  await s.store.saveAnswer(A, "EXIT", { raw: "yes" });
  await s.store.saveAnswer(A, "TEMP", { raw: "4" });
  const queued = await s.store.submit(A);
  assert.equal(queued.ok, true);
  let opened = await s.store.open(A);
  assert.equal(opened.submitState, "queued");
  assert.notEqual(opened.record.executionStatus, "submitted");
  const r = await s.engine.run();
  assert.equal(r.applied, 3);
  opened = await s.store.open(A);
  assert.equal(opened.submitState, "submitted");
  assert.equal(opened.readOnly, true);
  assert.equal(s.server.submitted, true);
  await assert.rejects(s.store.saveAnswer(A, "TEMP", { raw: "5" }), /no longer be edited/);
});

test("submit is not sent while an answer is in conflict, and local checks block an incomplete checklist", async () => {
  const s = await prepared();
  const incomplete = await s.store.submit(A);
  assert.equal(incomplete.ok, false);
  assert.deepEqual(Object.keys(incomplete.problems).sort(), ["EXIT", "TEMP"]);
  await s.store.saveAnswer(A, "EXIT", { raw: "yes" });
  await s.store.saveAnswer(A, "TEMP", { raw: "4" });
  await s.engine.run();
  s.server.edit(ITEMS.temp, "9"); // another device
  await s.store.saveAnswer(A, "TEMP", { raw: "6" });
  await s.store.submit(A);
  const r = await s.engine.run();
  assert.equal(r.conflict, 1);
  assert.equal(s.server.submitted, false);
  const ops = await s.storage.getAll(STORES.operations);
  assert.ok(ops.some((o) => o.type === "submit" && o.state === OP_STATE.pending));
});

test("operations survive a reload: interrupted 'sending' entries return to pending", async () => {
  const s = await prepared();
  await s.store.saveAnswer(A, "TEMP", { raw: "4" });
  const [op] = await s.storage.getAll(STORES.operations);
  await s.storage.put(STORES.operations, { ...op, state: OP_STATE.sending });
  assert.equal(await recoverInterrupted(s.ctx), 1);
  assert.equal((await s.storage.getAll(STORES.operations))[0].state, OP_STATE.pending);
});

test("authorization problems stop the run without losing or counting the queue", async () => {
  const s = await prepared();
  await s.store.saveAnswer(A, "TEMP", { raw: "4" });
  s.server.session = null; // signed out / session revoked
  const r = await s.engine.run();
  assert.equal(r.reason, "auth");
  assert.equal(s.engine.status, "auth");
  assert.equal((await queueSummary(s.ctx)).pending, 1);
  s.server.session = { user_id: "cccccccc-cccc-4ccc-8ccc-cccccccccccc", organization_id: ORG, csrf_token: "t" }; // somebody else
  assert.equal((await s.engine.run()).reason, "account_changed");
  assert.equal((await queueSummary(s.ctx)).pending, 1);
  assert.equal(s.server.requests.filter((x) => x.url.endsWith("/sync/")).length, 0);
});

test("the wire format carries the operation id, sequence and base revision - and no authority", () => {
  const wire = toWire({ opId: "o", type: "upsert_response", seq: 3, assignmentId: A, versionId: "v", itemId: ITEMS.temp, payload: { value: "4", not_applicable: false, comment: "" }, baseRevision: 2, createdAt: 5 });
  assert.deepEqual(Object.keys(wire).sort(), ["assignment_id", "base_revision", "client_created_at", "comment", "item_id", "not_applicable", "operation_id", "schema_version", "seq", "type", "value", "version_id"]);
  assert.equal(toWire({ opId: "o", type: "submit", seq: 4, assignmentId: A, versionId: "v", payload: {} }).item_id, undefined);
});

// ------------------------------------------------------------------------------------------------------ conflicts
async function conflicted() {
  const s = await prepared();
  await s.store.saveAnswer(A, "TEMP", { raw: "4" });
  await s.engine.run();
  s.server.edit(ITEMS.temp, "8");
  await s.store.saveAnswer(A, "TEMP", { raw: "2" });
  const r = await s.engine.run();
  assert.equal(r.conflict, 1);
  return s;
}

test("a conflict keeps both versions, blocks editing that answer and is not auto-merged", async () => {
  const s = await conflicted();
  const [c] = await s.conflicts.list();
  assert.equal(c.local.value, "2.0");
  assert.equal(c.server.value, "8");
  assert.equal(c.code, "revision_mismatch");
  assert.equal(s.server.rows.get(ITEMS.temp).value, "8");
  const opened = await s.store.open(A);
  assert.equal(opened.responses.TEMP.syncState, SYNC_STATE.conflict);
  assert.equal(opened.counts.conflict, 1);
  await assert.rejects(s.store.saveAnswer(A, "TEMP", { raw: "3" }), /conflict/);
});

test("keep server: the server value replaces the local one and the conflict is closed", async () => {
  const s = await conflicted();
  const [c] = await s.conflicts.list();
  const r = await s.conflicts.keepServer(c);
  assert.equal(r.status, "applied");
  const opened = await s.store.open(A);
  assert.equal(opened.responses.TEMP.value, "8");
  assert.equal(opened.responses.TEMP.syncState, SYNC_STATE.synced);
  assert.equal(opened.responses.TEMP.baseRevision, 2);
  assert.equal((await s.conflicts.list()).length, 0);
  assert.equal((await queueSummary(s.ctx)).total, 0);
});

test("use mine: re-sent against the server's current revision and then synchronized", async () => {
  const s = await conflicted();
  const [c] = await s.conflicts.list();
  const r = await s.conflicts.useLocal(c);
  assert.equal(r.status, "applied");
  assert.equal(s.server.rows.get(ITEMS.temp).value, "2.0");
  const opened = await s.store.open(A);
  assert.equal(opened.responses.TEMP.syncState, SYNC_STATE.synced);
  assert.equal(opened.responses.TEMP.baseRevision, 3);
  assert.equal((await s.conflicts.list()).length, 0);
});

test("use mine against a server that moved again conflicts again and keeps everything", async () => {
  const s = await conflicted();
  const [c] = await s.conflicts.list();
  s.server.edit(ITEMS.temp, "9");
  const r = await s.conflicts.useLocal(c);
  assert.equal(r.status, "conflict");
  const [again] = await s.conflicts.list();
  assert.equal(again.server.value, "9");
  assert.equal(again.local.value, "2.0");
});

test("a submitted execution's conflict is terminal: kept, exportable, discarded only on request", async () => {
  const s = await prepared();
  await s.store.saveAnswer(A, "TEMP", { raw: "4" });
  s.server.submitted = true;
  await s.engine.run();
  const [c] = await s.conflicts.list();
  assert.equal(c.code, "execution_submitted");
  assert.equal(s.conflicts.resolvable(c), false);
  assert.equal(s.conflicts.isTerminal(c), true);
  const opened = await s.store.open(A);
  assert.equal(opened.readOnly, true);
  assert.equal(opened.responses.TEMP.value, "4.0"); // preserved
  const file = await s.conflicts.recoveryExport(A);
  assert.equal(file.responses[0].value, "4.0");
  assert.equal(file.conflicts[0].reason, "execution_submitted");
  await s.conflicts.discard(c);
  assert.equal((await s.store.open(A)).responses.TEMP, undefined);
});

// ------------------------------------------------------------------------------------------------------ privacy / cleanup
test("accounts are isolated by namespace", async () => {
  const storage = new MemoryStorage();
  const mine = await prepared({ storage });
  await mine.store.saveAnswer(A, "EXIT", { raw: "yes" });
  const other = setup({ storage, ns: `cccccccc-cccc-4ccc-8ccc-cccccccccccc:${ORG}` });
  assert.equal(await other.cache.get(A), null);
  assert.equal(await other.store.open(A), null);
  assert.deepEqual(await other.cache.list(), []);
  assert.equal((await queueSummary(other.ctx)).total, 0);
});

test("removing a copy with unsynchronized work needs an explicit discard", async () => {
  const s = await prepared();
  await s.store.saveAnswer(A, "EXIT", { raw: "yes" });
  const refused = await s.cache.remove(A);
  assert.deepEqual(refused, { removed: false, unsynced: 2 });
  assert.ok(await s.cache.get(A));
  await s.engine.run();
  assert.equal((await s.cache.remove(A)).removed, true);
  assert.equal(await s.cache.get(A), null);
  assert.equal((await s.storage.getAll(STORES.responses)).length, 0);
});

test("retention only removes fully synchronized copies", async () => {
  const s = await prepared();
  s.clock.t += 40 * 86400000;
  assert.deepEqual(await s.cache.purgeExpired(14), [A]);
  const p2 = await prepared();
  await p2.store.saveAnswer(A, "EXIT", { raw: "yes" });
  p2.clock.t += 40 * 86400000;
  assert.deepEqual(await p2.cache.purgeExpired(14), []);
  assert.ok(await p2.cache.get(A));
});

test("clearing other accounts' cache keeps their unsynchronized work", async () => {
  const storage = new MemoryStorage();
  const theirs = await prepared({ storage });
  await theirs.store.saveAnswer(A, "EXIT", { raw: "yes" });
  const kept = await purgeOtherAccounts(storage, `cccccccc-cccc-4ccc-8ccc-cccccccccccc:${ORG}`);
  assert.deepEqual(kept, { removed: 0, kept: 1 });
  await theirs.engine.run();
  assert.deepEqual(await purgeOtherAccounts(storage, `cccccccc-cccc-4ccc-8ccc-cccccccccccc:${ORG}`), { removed: 1, kept: 0 });
  assert.equal((await storage.getAll(STORES.packages)).length, 0);
});

test("the remembered account stops opening after sign-out and holds no secrets", () => {
  const mem = new Map();
  const storage = { getItem: (k) => (mem.has(k) ? mem.get(k) : null), setItem: (k, v) => mem.set(k, v), removeItem: (k) => mem.delete(k) };
  assert.equal(recallAccount(storage), null);
  rememberAccount(storage, ACCOUNT);
  assert.deepEqual(recallAccount(storage), ACCOUNT);
  endSession(storage);
  assert.equal(recallAccount(storage), null);
  assert.deepEqual(Object.keys(JSON.parse(mem.get("cp-offline-account"))).sort(), ["organizationId", "userId"]);
});

test("connectivity follows real request outcomes", async () => {
  const server = new FakeServer();
  const c = new Connectivity({ fetchImpl: async () => { throw new TypeError("down"); } });
  const seen = [];
  c.subscribe((s) => seen.push(s));
  assert.equal(c.state, "unknown");
  assert.equal(await c.probe(), false);
  c.fetchImpl = async () => ({ status: 200 });
  assert.equal(await c.probe(), true);
  assert.deepEqual(seen, ["offline", "online"]);
  assert.equal(server.requests.length, 0);
});




test("the default fetch is called unbound (browsers throw 'Illegal invocation' if it is called as a method)", async () => {
  const server = new FakeServer();
  const original = globalThis.fetch;
  globalThis.fetch = function (...args) {
    if (this !== undefined && this !== globalThis) throw new TypeError("Illegal invocation");
    return server.fetch(...args);
  };
  try {
    const ctx = { storage: new MemoryStorage(), ns: NS, now: () => 1 };
    assert.equal((await new PackageCache({ ctx }).prepare(A)).status, "ready");
    assert.equal((await new Connectivity().probe()), true);
    assert.equal((await fetchSession()).status, "ok");
    const engine = new SyncEngine({ ctx, connectivity: new Connectivity(), getSession: () => fetchSession(), locks: null, setTimer: () => 0, clearTimer: () => {} });
    assert.equal((await engine.run()).ok, true);
    assert.ok(new ConflictHandler({ ctx, getSession: () => fetchSession() }).fetchImpl);
  } finally {
    globalThis.fetch = original;
  }
});