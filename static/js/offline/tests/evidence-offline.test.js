import { test } from "node:test";
import assert from "node:assert/strict";
import { MemoryStorage } from "../memory-store.js";
import { StorageFailure } from "../database.js";
import { Connectivity } from "../connectivity.js";
import { PackageCache, unsyncedCount } from "../package-cache.js";
import { ExecutionStore } from "../execution-store.js";
import { SyncEngine } from "../sync-engine.js";
import { ConflictHandler } from "../conflict-handler.js";
import { fetchSession } from "../account.js";
import { EVIDENCE_STATE as S, OP_STATE, STORES } from "../schema.js";
import { MAX_ATTEMPTS } from "../sync-queue.js";
import { A, FakeServer, ITEMS, NS, fixturePackage } from "./fake-server.js";

const jpeg = (name = "site.jpg", size = 2048) => new File([new Uint8Array(size).fill(7)], name, { type: "image/jpeg" });

function evidencePackage() {
  const base = fixturePackage();
  base.items.push({ id: ITEMS.photo, code: "PHOTO", section: "S1", question: "Photo of the exit", help_text: "", instructions: "", answer_type: "photo",
    config: { max_files: 2, max_size_mb: 5, allowed_extensions: ["jpg", "jpeg", "png", "webp"] }, required: true, sort_order: 4, options: [] });
  base.items[1].required = false; // keep the sample small: EXIT and PHOTO are required
  return base;
}

async function prepared(opts = {}) {
  const server = opts.server || new FakeServer(evidencePackage());
  const clock = { t: 1_000_000 };
  const ctx = { storage: opts.storage || new MemoryStorage(), ns: opts.ns || NS, now: () => clock.t };
  const connectivity = new Connectivity({ fetchImpl: server.fetch });
  const getSession = async () => fetchSession(server.fetch);
  const cache = new PackageCache({ ctx, fetchImpl: server.fetch });
  assert.equal((await cache.prepare(A)).status, "ready");
  const store = new ExecutionStore({ ctx });
  const engine = new SyncEngine({ ctx, connectivity, getSession, fetchImpl: server.fetch, locks: null, setTimer: () => 0, clearTimer: () => {} });
  const conflicts = new ConflictHandler({ ctx, getSession, fetchImpl: server.fetch, connectivity });
  const photo = async () => {
    const state = await store.open(A);
    return { item: state.pkg.items.find((i) => i.code === "PHOTO"), result: state.evaluation.items.PHOTO };
  };
  return { server, ctx, clock, cache, store, engine, conflicts, storage: ctx.storage, photo };
}

async function addPhoto(s, file = jpeg(), caption = "") {
  const { item, result } = await s.photo();
  return s.store.evidence.add(A, item, result, file, { caption });
}

test("a captured file is saved on this device as a Blob before anything is said about uploading", async () => {
  const s = await prepared();
  const r = await addPhoto(s);
  assert.equal(r.ok, true);
  const [entry] = await s.store.evidence.list(A);
  assert.equal(entry.state, S.local);
  assert.ok(entry.blob instanceof Blob);
  assert.equal(entry.size, 2048);
  assert.match(entry.sha256, /^[0-9a-f]{64}$/);
  assert.match(entry.id, /^[0-9a-f-]{36}$/); // the stable operation id
  assert.equal(entry.ns, NS);
});

test("a failed IndexedDB write stores nothing and is never reported as saved", async () => {
  const s = await prepared();
  s.storage.failNextWrite = new StorageFailure("quota", "This device has no storage space left.");
  await assert.rejects(addPhoto(s), /no storage space/);
  assert.equal((await s.store.evidence.list(A)).length, 0);
});

test("local validation refuses the wrong type, size, count and a missing signer name", async () => {
  const s = await prepared();
  const bad = async (file, code) => assert.equal((await addPhoto(s, file)).errors[0].code, code);
  await bad(new File([new Uint8Array(10)], "run.exe", { type: "application/octet-stream" }), "type_not_allowed");
  await bad(new File([new Uint8Array(10)], "doc.pdf", { type: "application/pdf" }), "type_not_allowed"); // a photo question needs an image
  await bad(new File([new Uint8Array(10)], "pic.png", { type: "image/jpeg" }), "content_mismatch");
  await bad(new File([], "empty.jpg", { type: "image/jpeg" }), "empty_file");
  await bad(new File([new Uint8Array(11 * 1024 * 1024)], "huge.jpg", { type: "image/jpeg" }), "file_too_large");
  assert.equal((await addPhoto(s, jpeg("../../etc/passwd.jpg"))).entry.name, "passwd.jpg"); // a display name, never a path
  assert.equal((await addPhoto(s, jpeg("b.jpg"))).ok, true);
  await bad(jpeg("c.jpg"), "too_many_files"); // the question allows two
});

test("evidence questions are answered by files: the local check blocks submission until one is attached", async () => {
  const s = await prepared();
  await s.store.saveAnswer(A, "EXIT", { raw: "yes" });
  let opened = await s.store.open(A);
  assert.deepEqual(Object.keys(opened.problems), ["PHOTO"]);
  assert.equal((await s.store.submit(A)).ok, false);
  await addPhoto(s);
  opened = await s.store.open(A);
  assert.deepEqual(opened.problems, {});
  assert.equal(opened.evaluation.items.PHOTO.answer.status, "answered");
  assert.equal(opened.progress.percent, 100);
});

test("the upload sends the stable operation id and checksum, and the file is only 'available' when the server says so", async () => {
  const s = await prepared();
  await addPhoto(s);
  const [entry] = await s.store.evidence.list(A);
  const result = await s.engine.run();
  assert.equal(result.ok, true);
  const request = s.server.requests.find((r) => r.url.endsWith("/evidence/uploads/"));
  assert.equal(request.init.body.get("operation_id"), entry.id);
  assert.equal(request.init.body.get("client_sha256"), entry.sha256);
  assert.equal(request.init.body.get("assignment_id"), A);
  assert.equal(request.init.headers["X-CSRFToken"], "tok");
  assert.equal(request.init.headers["Content-Type"], undefined); // the browser must set the multipart boundary
  const [after] = await s.store.evidence.list(A);
  assert.equal(after.state, S.available);
  assert.equal(after.blob, null); // released only after the server acknowledged durable receipt
  assert.ok(after.serverAttachmentId);
});

test("a file waiting for the security check is 'received', not verified, and later becomes available", async () => {
  const server = new FakeServer(evidencePackage());
  server.evidenceStatus = "pending_scan";
  const s = await prepared({ server });
  await addPhoto(s);
  await s.engine.run();
  let [entry] = await s.store.evidence.list(A);
  assert.equal(entry.state, S.received);
  assert.equal(await unsyncedCount(s.ctx, A), 0); // the server holds it: nothing local is at risk
  for (const a of server.assets.values()) a.status = "available";
  await s.engine.run();
  [entry] = await s.store.evidence.list(A);
  assert.equal(entry.state, S.available);
});

test("a lost response is retried with the same operation id and the server keeps one file", async () => {
  const s = await prepared();
  await addPhoto(s);
  s.server.evidenceDrop = true;
  const lost = await s.engine.run();
  assert.equal(lost.ok, false);
  let [entry] = await s.store.evidence.list(A);
  assert.equal(entry.state, S.local); // still here, Blob intact: sent is not acknowledged
  assert.ok(entry.blob);
  assert.equal(s.server.assets.size, 1);
  s.server.evidenceDrop = false;
  s.clock.t += 10 * 60 * 1000;
  assert.equal((await s.engine.run()).ok, true);
  [entry] = await s.store.evidence.list(A);
  assert.equal(entry.state, S.available);
  assert.equal(s.server.assets.size, 1);
  assert.equal(s.server.uploadCount, 2);
});

test("an interrupted 'uploading' entry returns to local after a reload", async () => {
  const s = await prepared();
  await addPhoto(s);
  const [entry] = await s.store.evidence.list(A);
  await s.storage.put(STORES.evidence, { ...entry, state: S.uploading });
  await s.engine.start();
  assert.equal((await s.store.evidence.list(A))[0].state, S.available);
  assert.equal(s.server.assets.size, 1);
});

test("a refused file is rejected with the server's reason, keeps its Blob, and can be removed by the user", async () => {
  const s = await prepared();
  s.server.evidenceError = { status: 400, code: "content_mismatch", message: "The file's content does not match its type." };
  await addPhoto(s);
  await s.engine.run();
  const [entry] = await s.store.evidence.list(A);
  assert.equal(entry.state, S.rejected);
  assert.match(entry.error, /does not match/);
  assert.ok(entry.blob);
  assert.equal(await unsyncedCount(s.ctx, A), 1); // protected from silent deletion
  assert.equal((await s.cache.remove(A)).removed, false);
  await s.store.evidence.removeLocal(entry);
  assert.equal((await s.store.evidence.list(A)).length, 0);
});

test("a corrupted transfer (checksum mismatch) is retried, not rejected", async () => {
  const s = await prepared();
  s.server.evidenceError = { status: 400, code: "checksum_mismatch", message: "The file did not arrive intact." };
  await addPhoto(s);
  await s.engine.run();
  const [entry] = await s.store.evidence.list(A);
  assert.equal(entry.state, S.local);
  assert.equal(entry.attempts, 1);
  s.server.evidenceError = null;
  s.clock.t += 10 * 60 * 1000;
  await s.engine.run();
  assert.equal((await s.store.evidence.list(A))[0].state, S.available);
});

test("server errors back off, then fail until Sync now - the Blob is never dropped", async () => {
  const s = await prepared();
  await addPhoto(s);
  s.server.evidenceError = { status: 503, code: "", message: "unavailable" };
  for (let i = 0; i < MAX_ATTEMPTS; i += 1) {
    s.clock.t += 10 * 60 * 1000;
    await s.engine.run();
  }
  let [entry] = await s.store.evidence.list(A);
  assert.equal(entry.state, S.failed);
  assert.ok(entry.blob);
  s.server.evidenceError = null;
  await s.engine.run({ force: true });
  [entry] = await s.store.evidence.list(A);
  assert.equal(entry.state, S.available);
});

test("an expired session keeps the file on the device and stops without counting an attempt", async () => {
  const s = await prepared();
  await addPhoto(s);
  s.server.evidenceError = { status: 401, code: "", message: "" };
  const r = await s.engine.run();
  assert.equal(r.reason, "auth");
  const [entry] = await s.store.evidence.list(A);
  assert.equal(entry.state, S.local);
  assert.equal(entry.attempts, 0);
  assert.ok(entry.blob);
});

test("submit waits for evidence to be verified, then follows it", async () => {
  const server = new FakeServer(evidencePackage());
  server.evidenceStatus = "pending_scan";
  const s = await prepared({ server });
  await s.store.saveAnswer(A, "EXIT", { raw: "yes" });
  await addPhoto(s);
  assert.equal((await s.store.submit(A)).ok, true);
  const first = await s.engine.run();
  assert.equal(first.applied, 1); // the answer went through...
  assert.equal(server.submitted, false); // ...but the submit is held: the file is not verified yet
  const ops = await s.storage.getAll(STORES.operations);
  assert.ok(ops.some((o) => o.type === "submit" && o.state === OP_STATE.pending));
  for (const a of server.assets.values()) a.status = "available";
  const second = await s.engine.run();
  assert.equal(second.applied, 1);
  assert.equal(server.submitted, true);
  assert.equal((await s.store.open(A)).submitState, "submitted");
});

test("evidence is namespaced per account and never shown to another user of the same browser", async () => {
  const storage = new MemoryStorage();
  const mine = await prepared({ storage });
  await addPhoto(mine);
  const other = await prepared({ storage, ns: "cccccccc-cccc-4ccc-8ccc-cccccccccccc:bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb" });
  assert.equal((await other.store.evidence.list(A)).length, 0);
  assert.equal((await mine.store.evidence.list(A)).length, 1);
});

test("removing a copy discards its evidence only after the explicit discard", async () => {
  const s = await prepared();
  await addPhoto(s);
  assert.equal((await s.cache.remove(A)).removed, false);
  assert.ok((await s.store.evidence.list(A)).length);
  assert.equal((await s.cache.remove(A, { discardUnsynced: true })).removed, true);
  assert.equal((await s.store.evidence.list(A)).length, 0);
});

test("the recovery export lists the files without embedding their contents", async () => {
  const s = await prepared();
  await addPhoto(s);
  const file = await s.conflicts.recoveryExport(A);
  assert.equal(file.evidence.length, 1);
  assert.equal(file.evidence[0].name, "site.jpg");
  assert.ok(!JSON.stringify(file).includes("blob"));
});
