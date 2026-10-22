/* Service worker registration and the hooks the offline engine needs from it.
 *
 * Background Sync is an ENHANCEMENT: where the browser supports it, a sync is requested after local changes so the worker can wake the
 * page to synchronize; everywhere else (and whenever no page is open) synchronization simply happens at the next explicit trigger.
 */
export async function registerServiceWorker() {
  if (!("serviceWorker" in navigator)) {
    return null;
  }
  try {
    return await navigator.serviceWorker.register("/service-worker.js", { scope: "/" });
  } catch (error) {
    return null;
  }
}

export async function requestBackgroundSync() {
  try {
    const registration = await navigator.serviceWorker.ready;
    if (registration && registration.sync) {
      await registration.sync.register("checklist-sync");
      return true;
    }
  } catch (error) {
    /* unsupported or denied: fine */
  }
  return false;
}

/** The worker tells an open page that the browser granted a background sync: the page runs its (single-flight) engine. */
export function onSyncRequested(handler) {
  if (!("serviceWorker" in navigator)) {
    return;
  }
  navigator.serviceWorker.addEventListener("message", (event) => {
    if (event.data && event.data.type === "sync-requested") {
      handler();
    }
  });
}

/** Ask the worker's cache to hold the app shell resources this page just loaded (so the shell opens offline after a first online visit). */
export async function warmShellCache() {
  try {
    await navigator.serviceWorker.ready;
    const urls = [...document.querySelectorAll('link[rel="stylesheet"][href], script[src]')].map((n) => n.getAttribute("href") || n.getAttribute("src"));
    await Promise.all(urls.map((u) => fetch(u, { credentials: "same-origin" }).catch(() => null)));
  } catch (error) {
    /* best effort */
  }
}
