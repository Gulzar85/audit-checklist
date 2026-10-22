# Offline security and privacy

## What is stored locally

| Where | What | Never |
|---|---|---|
| IndexedDB (`checklist-offline`) | the prepared package (checklist definition, assignment metadata, location path, instructions), the user's answers and comments, the sync queue, conflicts | passwords, OAuth/MSAL tokens, session secrets, permissions, roles, other users' data |
| Cache Storage (`cp-static-v6`) | the public app shell, the offline fallback page, CSS/JS/icons | authenticated HTML, API responses, anything with user data |
| `localStorage` | `cp-offline-account` (user id + organization id), `cp-offline-session-ended` (flag), the retention-days setting | tokens, answers |
| memory only | the CSRF token returned by `GET /offline/session/` | - |

## Controls

* **Account namespace** on every record (`userId:organizationId`). Switching account or organization opens a different namespace; the
  previous one is neither read nor shown. Cache management can clear *synchronized* data of other accounts; their unsynchronized work is
  kept for them.
* **Sign-out** marks the remembered account "ended": the offline workspace then shows nothing until a server-verified sign-in. (The data
  stays on disk for its owner - synchronized copies can be removed from *Storage*.)
* **Identity is re-asserted by the server** before every sync; a different account stops the run and keeps the queue untouched.
* **Authorization is the server's**: a package is only served to a user who may execute that assignment (404 otherwise); every sync
  operation is re-authorized at its location. Offline permissions never override current server policy.
* **Retention**: synchronized copies expire after a configurable number of days (default 14); unsynchronized work is **never** removed
  automatically. Removal of a copy that holds unsynchronized work needs an explicit warning and offers a recovery download first.
* **Service worker** allowlist ([service-worker-caching.md](service-worker-caching.md)); responses marked `no-store`/`private` are not stored.
* **No code execution from data**: the client never evaluates strings as code and never writes data as HTML (`textContent` only); the
  rule engine interprets declarative JSON; the CSP forbids inline script.
* **Server hardening**: payload, batch and value limits, a throttle scope, strict operation schema (unknown fields rejected),
  no-store responses, receipts scoped per user, no endpoint that touches browser storage.

## Residual risks (stated honestly)

* IndexedDB and Cache Storage are **not encrypted** by this application and are readable by anyone with access to the browser profile or
  device (malware, a shared OS account, devtools). Client-side encryption is not offered because a key held by the same page would not
  change that. On shared devices: sign out, remove checklists when done, use separate OS profiles.
* A user (or script in their profile) can alter local data. That is why nothing local is trusted: the server re-validates everything.
* Offline operational data (assignment text, locations, answers) remains on the device until synchronized and removed or expired.
* Browsers may evict storage under pressure (notably Safari after inactivity); synchronized data can be re-downloaded, unsynchronized data
  cannot - synchronize promptly.
