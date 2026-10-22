/* Server reachability, judged by real request outcomes - not by navigator.onLine alone (which only says a network interface exists).
 *
 * ``report(true)`` is called whenever a request to the server succeeded in reaching it (any HTTP answer), ``report(false)`` when a
 * request failed at the network level.  ``probe()`` makes one tiny unauthenticated request (the API health endpoint) and is only
 * called when the browser says it came back online or the user asks - there is no polling loop.
 */
export class Connectivity {
  constructor({ fetchImpl = (...args) => globalThis.fetch(...args), healthUrl = "/api/v1/health/", timeoutMs = 4000, now = () => Date.now() } = {}) {
    this.fetchImpl = fetchImpl;
    this.healthUrl = healthUrl;
    this.timeoutMs = timeoutMs;
    this.now = now;
    this.state = "unknown"; // "unknown" | "online" | "offline"
    this.changedAt = now();
    this.listeners = new Set();
  }

  subscribe(listener) {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  }

  report(reachable) {
    const next = reachable ? "online" : "offline";
    if (next !== this.state) {
      this.state = next;
      this.changedAt = this.now();
      this.listeners.forEach((l) => l(next));
    }
  }

  async probe() {
    const controller = typeof AbortController === "function" ? new AbortController() : null;
    const timer = controller ? setTimeout(() => controller.abort(), this.timeoutMs) : null;
    try {
      const response = await this.fetchImpl(this.healthUrl, { cache: "no-store", credentials: "omit", signal: controller ? controller.signal : undefined });
      this.report(Boolean(response)); // any HTTP answer (even a 5xx) proves the server was reached
      return this.state === "online";
    } catch (error) {
      this.report(false);
      return false;
    } finally {
      if (timer) {
        clearTimeout(timer);
      }
    }
  }

  /** Wire browser events: they are only hints that trigger a probe. */
  attach(target = globalThis) {
    if (target && target.addEventListener) {
      target.addEventListener("online", () => this.probe());
      target.addEventListener("offline", () => this.report(false));
    }
    return this;
  }
}

