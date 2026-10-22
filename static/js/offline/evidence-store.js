/* Offline evidence (Phase 11): files captured or chosen on the device are stored as Blobs in IndexedDB, queued, and uploaded through the
 * ordinary evidence endpoint (POST /api/v1/evidence/uploads/) - there is no second sync protocol.
 *
 * States (what the user is told; only the server can move an entry to "available"):
 *   local      saved on this device, waiting to upload           (the Blob is here)
 *   uploading  the request is in flight
 *   received   the server has the bytes durably; its security check is pending (the Blob is released: the server owns the file now)
 *   available  the server verified it - this is the only state that counts as submitted evidence
 *   failed     upload retries exhausted (transient errors); "Sync now" starts over
 *   rejected   the server refused the file (type, content, size, permission...): needs the user; the Blob is kept for recovery
 *   quarantined the server's security check flagged it
 * An entry is never deleted because a request was sent; the local Blob is released only after the server acknowledged receipt.
 * Idempotency: the entry's id is the upload's ``operation_id`` - a retry after a lost response returns the original attachment.
 */
import { EVIDENCE_STATE as S, STORES, newId, validateEvidenceRecord } from "./schema.js";
import { BASE_BACKOFF_MS, MAX_ATTEMPTS, MAX_BACKOFF_MS, backoffMs, packageKey } from "./sync-queue.js";

export const APPROVED_TYPES = { jpg: "image/jpeg", jpeg: "image/jpeg", png: "image/png", webp: "image/webp", pdf: "application/pdf" };
export const DEFAULT_LIMITS = { max_file_bytes: 10 * 1024 * 1024, max_per_target: 10 };
const LIVE = [S.local, S.uploading, S.received, S.available, S.failed]; // entries that count toward the local evidence for a question
const PENDING_STATES = [S.local, S.uploading, S.failed, S.rejected]; // "not yet safely on the server": protects against silent deletion

export function sanitizeName(raw) {
  const base = String(raw || "").split(/[\\/]/).pop().replace(/[^A-Za-z0-9._ ()-]/g, "_").replace(/^\.+/, "").trim().slice(0, 120);
  return base || "file";
}

export const extensionOf = (name) => (name.includes(".") ? name.split(".").pop().toLowerCase() : "");

async function sha256Hex(buffer) {
  const digest = await crypto.subtle.digest("SHA-256", buffer);
  return Array.from(new Uint8Array(digest), (b) => b.toString(16).padStart(2, "0")).join("");
}

/** Which evidence kind an item takes: its own type for photo/file/signature items, otherwise the rule's kinds (or ``null`` = any). */
export function itemKind(item, evalResult) {
  if (["photo", "file", "signature"].includes(item.answer_type)) {
    return item.answer_type;
  }
  const kinds = evalResult && evalResult.evidence ? evalResult.evidence.kinds : [];
  return kinds.length === 1 ? kinds[0] : null;
}

export function takesEvidence(item, evalResult) {
  return ["photo", "file", "signature"].includes(item.answer_type) || !!(evalResult && evalResult.evidence);
}

export class EvidenceStore {
  constructor({ ctx, limits = DEFAULT_LIMITS }) {
    this.ctx = ctx;
    this.limits = { ...DEFAULT_LIMITS, ...limits };
  }

  async list(assignmentId) {
    const rows = await this.ctx.storage.getAllByIndex(STORES.evidence, "byAssignment", assignmentId);
    return rows.filter((r) => r.ns === this.ctx.ns).sort((a, b) => a.createdAt - b.createdAt);
  }

  /** Validate and store a chosen/captured file.  Resolves only after the IndexedDB transaction committed ("saved on this device"). */
  async add(assignmentId, item, evalResult, file, { caption = "" } = {}) {
    const { ctx } = this;
    const pkg = await ctx.storage.get(STORES.packages, packageKey(ctx.ns, assignmentId));
    if (!pkg || pkg.state !== "ready") {
      return { ok: false, errors: [{ code: "not_prepared", message: "This checklist is not prepared on this device." }] };
    }
    if (pkg.executionStatus === "submitted" || pkg.submitState === "submitted" || pkg.blockedReason) {
      return { ok: false, errors: [{ code: "read_only", message: "This checklist can no longer be changed." }] };
    }
    const name = sanitizeName(file.name);
    const ext = extensionOf(name);
    const kind = itemKind(item, evalResult);
    const config = ["photo", "file"].includes(item.answer_type) ? item.config : {};
    const bad = (code, message) => ({ ok: false, errors: [{ code, message }] });
    if (!(ext in APPROVED_TYPES)) {
      return bad("type_not_allowed", "This file type is not allowed. Allowed: JPG, PNG, WEBP, PDF.");
    }
    if (config.allowed_extensions && !config.allowed_extensions.includes(ext) && !(ext === "jpg" && config.allowed_extensions.includes("jpeg")) && !(ext === "jpeg" && config.allowed_extensions.includes("jpg"))) {
      return bad("type_not_allowed", "This question does not accept that file type.");
    }
    if ((kind === "photo" || kind === "signature") && ext === "pdf") {
      return bad("type_not_allowed", "This question needs an image.");
    }
    if (file.type && file.type !== APPROVED_TYPES[ext] && !(ext === "jpg" && file.type === "image/jpeg")) {
      return bad("content_mismatch", "The file's type does not match its name.");
    }
    if (!file.size) {
      return bad("empty_file", "The file is empty.");
    }
    if (file.size > this.limits.max_file_bytes) {
      return bad("file_too_large", `The file is larger than ${Math.round(this.limits.max_file_bytes / 1048576)} MB.`);
    }
    if (item.answer_type === "signature" && !String(caption).trim()) {
      return bad("signer_name", "Enter the signer's name.");
    }
    const existing = (await this.list(assignmentId)).filter((e) => e.itemId === item.id && LIVE.includes(e.state));
    const maxFiles = Math.min(Number(config.max_files) || this.limits.max_per_target, this.limits.max_per_target);
    if (existing.length >= maxFiles) {
      return bad("too_many_files", `At most ${maxFiles} file${maxFiles === 1 ? "" : "s"} can be attached to one question.`);
    }
    const sha256 = await sha256Hex(await file.arrayBuffer());
    const entry = {
      id: newId(), ns: ctx.ns, assignmentId, versionId: pkg.versionId, itemId: item.id, itemCode: item.code, kind: kind || (ext === "pdf" ? "file" : "photo"), name, size: file.size,
      type: APPROVED_TYPES[ext], blob: file, sha256, caption: String(caption || "").slice(0, 200), state: S.local, attempts: 0, nextAttemptAt: 0, error: null, createdAt: ctx.now(),
      serverAttachmentId: null, serverAssetId: null, serverStatus: null,
    };
    validateEvidenceRecord(entry);
    await ctx.storage.put(STORES.evidence, entry);
    return { ok: true, entry };
  }

  /** Remove an entry the server has never accepted (or that it refused).  Accepted files are removed through the API (detach). */
  async removeLocal(entry) {
    if (![S.local, S.failed, S.rejected].includes(entry.state)) {
      throw new Error("This file is already on the server; remove it while online.");
    }
    await this.ctx.storage.delete(STORES.evidence, entry.id);
  }
}

// ------------------------------------------------------------------------------------------------------ sync side
export async function pendingUploads(ctx, now) {
  const rows = (await ctx.storage.getAllByIndex(STORES.evidence, "byNs", ctx.ns)).filter((e) => e.state === S.local && (e.nextAttemptAt || 0) <= now);
  return rows.sort((a, b) => a.createdAt - b.createdAt);
}

export async function receivedEntries(ctx) {
  return (await ctx.storage.getAllByIndex(STORES.evidence, "byNs", ctx.ns)).filter((e) => e.state === S.received && e.serverAssetId);
}

export async function holdsSubmit(ctx, assignmentId) {
  const rows = (await ctx.storage.getAllByIndex(STORES.evidence, "byAssignment", assignmentId)).filter((e) => e.ns === ctx.ns);
  return rows.some((e) => [S.local, S.uploading, S.received, S.failed].includes(e.state)); // not yet verified: submit waits
}

export async function recoverUploading(ctx) {
  const stuck = (await ctx.storage.getAllByIndex(STORES.evidence, "byNs", ctx.ns)).filter((e) => e.state === S.uploading);
  if (stuck.length) {
    await ctx.storage.batch(stuck.map((e) => ({ type: "put", store: STORES.evidence, record: { ...e, state: S.local } })));
  }
  return stuck.length;
}

export async function resetFailedEvidence(ctx) {
  const rows = (await ctx.storage.getAllByIndex(STORES.evidence, "byNs", ctx.ns)).filter((e) => e.state === S.failed || (e.state === S.local && e.nextAttemptAt));
  if (rows.length) {
    await ctx.storage.batch(rows.map((e) => ({ type: "put", store: STORES.evidence, record: { ...e, state: S.local, attempts: 0, nextAttemptAt: 0 } })));
  }
}

export function formDataFor(entry, executionId) {
  const form = new FormData();
  if (executionId) {
    form.append("execution_id", executionId);
  } else {
    form.append("assignment_id", entry.assignmentId);
  }
  form.append("item_id", entry.itemId);
  form.append("operation_id", entry.id); // the idempotency key: stable across retries
  form.append("client_sha256", entry.sha256); // the server verifies the bytes that arrived match
  form.append("captured_at", new Date(entry.createdAt).toISOString()); // diagnostic only
  if (entry.caption) {
    form.append("caption", entry.caption);
  }
  form.append("file", entry.blob, entry.name);
  return form;
}

async function transient(ctx, entry, message, retryAfterMs = 0) {
  const attempts = Math.max(0, (entry.attempts || 0) + 1);
  const exhausted = attempts >= MAX_ATTEMPTS;
  await ctx.storage.put(STORES.evidence, {
    ...entry, state: exhausted ? S.failed : S.local, attempts, error: message, nextAttemptAt: ctx.now() + Math.max(retryAfterMs, Math.min(MAX_BACKOFF_MS, BASE_BACKOFF_MS * 2 ** (attempts - 1))),
  });
  return exhausted ? "failed" : "retry";
}

/** Apply the server's answer to one upload.  Local state changes only here, after the response arrived. */
export async function applyUploadResponse(ctx, entry, response) {
  let body = null;
  try {
    body = await response.json();
  } catch (error) {
    body = null;
  }
  const data = body && body.data ? body.data : body;
  if (response.status === 201 || response.status === 200) {
    const asset = (data && data.asset) || {};
    const state = asset.status === "available" ? S.available : asset.status === "quarantined" ? S.quarantined : S.received;
    await ctx.storage.put(STORES.evidence, {
      ...entry, state, blob: null, serverAttachmentId: data.id, serverAssetId: asset.id, serverStatus: asset.status, error: null, attempts: 0,
    });
    const pkg = await ctx.storage.get(STORES.packages, packageKey(ctx.ns, entry.assignmentId));
    if (pkg && data.execution && !pkg.executionId) {
      await ctx.storage.put(STORES.packages, { ...pkg, executionId: data.execution });
    }
    return state === S.quarantined ? "rejected" : "ok";
  }
  const details = (body && body.error && body.error.details) || {};
  const code = details.code || "";
  const message = (details.file && details.file[0]) || (details.detail) || (body && body.error && body.error.message) || "The server did not accept this file.";
  if (response.status === 401) {
    return "auth";
  }
  if (response.status === 429 || response.status >= 500 || code === "checksum_mismatch" || code === "storage_failed") {
    const wait = Number(response.headers && response.headers.get && response.headers.get("Retry-After")) * 1000 || 0;
    return transient(ctx, entry, message, wait);
  }
  // 400 / 403 / 404 / 409 / 413: a final answer for this file - the user must act; the Blob stays for recovery.
  await ctx.storage.put(STORES.evidence, { ...entry, state: S.rejected, error: message, errorCode: code || String(response.status) });
  return "rejected";
}

export async function applyStatus(ctx, entry, response) {
  if (!response.ok) {
    return "unchanged";
  }
  const body = await response.json();
  const data = body && body.data ? body.data : body;
  if (data.status === "available") {
    await ctx.storage.put(STORES.evidence, { ...entry, state: S.available, serverStatus: "available" });
    return "available";
  }
  if (data.status === "quarantined") {
    await ctx.storage.put(STORES.evidence, { ...entry, state: S.quarantined, serverStatus: "quarantined", error: "The security check flagged this file; it does not count as evidence." });
    return "quarantined";
  }
  return "unchanged";
}

export const markTransient = transient;
export { PENDING_STATES, backoffMs };
