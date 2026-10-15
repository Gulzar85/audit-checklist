/* Checklist Platform - application shell behaviour.
 * No inline scripts are used; this file is self-hosted and covered by CSP. */
(function () {
  "use strict";

  /* Colour mode (light / dark / system) is server-rendered as <html data-theme> and changed
   * by the `appearance` component (components.js); nothing to initialise here, and there is
   * no flash because the first byte already carries the right mode. */

  function initIcons() {
    if (window.lucide && typeof window.lucide.createIcons === "function") {
      window.lucide.createIcons();
    }
  }

  function initServiceWorker() {
    if (!("serviceWorker" in navigator)) {
      return;
    }
    window.addEventListener("load", function () {
      navigator.serviceWorker.register("/service-worker.js", { scope: "/" }).catch(function () {
        /* registration failures must never break the app */
      });
    });
  }

  /* Brand-colour swatches: `<span data-swatch="#4F46E5">`.  The colour is applied
   * through the CSSOM, which the CSP allows (an inline style="" attribute is not). */
  function initSwatches(root) {
    (root || document).querySelectorAll("[data-swatch]").forEach(function (node) {
      var colour = node.getAttribute("data-swatch");
      if (/^#[0-9A-Fa-f]{6}$/.test(colour)) {
        node.style.backgroundColor = colour;
      }
    });
  }

  /* Location picker: send the visible text box's value as `q`.  The box has no
   * `name`, so it never pollutes a surrounding form's submission. */
  document.addEventListener("htmx:configRequest", function (event) {
    var source = event.detail.elt;
    if (source && source.hasAttribute && source.hasAttribute("data-picker-input")) {
      event.detail.parameters["q"] = source.value;
    }
  });

  /* HTMX failures must never leave a silent, half-updated page: reveal the
   * nearest `[data-htmx-error]` region (an aria-live alert) for the request. */
  function errorRegionFor(source) {
    var scope = source && source.closest ? source.closest("[data-htmx-scope]") : null;
    return (scope || document).querySelector("[data-htmx-error]");
  }
  ["htmx:responseError", "htmx:sendError", "htmx:timeout"].forEach(function (name) {
    document.addEventListener(name, function (event) {
      var region = errorRegionFor(event.detail && event.detail.elt);
      if (region) {
        region.hidden = false;
      }
    });
  });
  document.addEventListener("htmx:beforeRequest", function (event) {
    var region = errorRegionFor(event.detail && event.detail.elt);
    if (region) {
      region.hidden = true;
    }
  });

  document.addEventListener("DOMContentLoaded", function () {
    initIcons();
    initSwatches();
  });

  // Re-render icons and swatches after HTMX swaps content in.
  document.addEventListener("htmx:afterSwap", function (event) {
    initIcons();
    initSwatches(event.target);
  });

  initServiceWorker();
})();
