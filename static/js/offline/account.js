/* Identity for offline records.  Every local record carries an account namespace (user + organization) so two people sharing one
 * browser profile never see each other's data.  The identity is *asserted by the server* (GET /api/v1/offline/session/) whenever the
 * device is online; offline, only the last asserted account that has not signed out may open its own namespace.  No token, password or
 * permission is ever stored - only two opaque ids in localStorage (a convenience, not a credential).
 */
import { isUuid, namespaceOf } from "./schema.js";

export const ACCOUNT_KEY = "cp-offline-account";
export const ENDED_KEY = "cp-offline-session-ended";

export function rememberAccount(storage, account) {
  storage.setItem(ACCOUNT_KEY, JSON.stringify({ userId: account.userId, organizationId: account.organizationId }));
  storage.removeItem(ENDED_KEY);
}

/** Called when the user signs out (or the server reports no session): the namespace stays on disk but is no longer opened. */
export function endSession(storage) {
  storage.setItem(ENDED_KEY, String(Date.now()));
}

/** The account that may use the offline workspace *without* a server answer: remembered, and not signed out since. */
export function recallAccount(storage) {
  if (storage.getItem(ENDED_KEY)) {
    return null;
  }
  try {
    const value = JSON.parse(storage.getItem(ACCOUNT_KEY) || "null");
    return value && isUuid(value.userId) && isUuid(value.organizationId) ? value : null;
  } catch (error) {
    return null;
  }
}

/** Ask the server who is signed in.  ``{status: "ok", account, csrfToken}``, ``{status: "anonymous"}`` or ``{status: "offline"}``. */
export async function fetchSession(fetchImpl = (...args) => globalThis.fetch(...args)) {
  let response;
  try {
    response = await fetchImpl("/api/v1/offline/session/", { credentials: "same-origin", cache: "no-store", headers: { Accept: "application/json" } });
  } catch (error) {
    return { status: "offline" };
  }
  if (response.status === 401 || response.status === 403) {
    return { status: "anonymous" };
  }
  if (!response.ok) {
    return { status: "offline" };
  }
  const body = await response.json();
  const data = body && body.data ? body.data : body;
  if (!data || !isUuid(data.user_id)) {
    return { status: "anonymous" };
  }
  if (!isUuid(data.organization_id)) {
    return { status: "no_organization" };
  }
  return { status: "ok", account: { userId: data.user_id, organizationId: data.organization_id, organizationName: data.organization_name || "" }, csrfToken: data.csrf_token };
}

export const namespaceFor = (account) => namespaceOf(account);
