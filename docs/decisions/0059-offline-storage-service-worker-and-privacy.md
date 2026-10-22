# 0059. Offline storage, service-worker allowlist and privacy

- **Status:** Accepted
- **Date:** 2026-10-22

## Decision
* **IndexedDB** holds all offline data (versioned schema, additive migrations, atomic batches, namespaced by account); `localStorage`
  holds only two account ids, a sign-out flag and the retention setting; Cache Storage holds only public assets.
* **The offline shell is public and data-free** (`/offline/app/`): precacheable without leaking a session; data comes from IndexedDB or
  authenticated API calls. Authenticated HTML is never cached; `/auth/`, `/api/`, `/admin/` are never intercepted; `no-store`/`private`
  responses are not stored.
* **Identity is asserted by the server** before every sync; offline, only the last signed-in account that has not signed out opens its
  namespace and the UI says it is unverified. Sign-out stops the offline workspace from opening that data.
* **Unsynchronized work is never silently removed**: retention only removes synchronized copies; removal with pending work needs an
  explicit warning and offers a recovery export.
* No claim of encryption or guaranteed persistence; residual risks are documented ([offline-security-and-privacy.md](../offline-security-and-privacy.md)).

## Consequences
* Shared devices need user discipline (sign out, remove copies); the application cannot make browser storage private.
* A change to the asset set or the policy requires a `CACHE_VERSION` bump (tested).
