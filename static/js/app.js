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

  /* Modals / drawers: any control with data-modal-open="<dialog id>" opens that native <dialog>;
   * data-modal-close closes the one it sits in; a click on the backdrop (the dialog element
   * itself) closes it.  Focus trapping, Escape and inertness of the page come from the browser. */
  document.addEventListener("click", function (event) {
    var opener = event.target.closest ? event.target.closest("[data-modal-open]") : null;
    if (opener) {
      var dialog = document.getElementById(opener.getAttribute("data-modal-open"));
      if (dialog && typeof dialog.showModal === "function" && !dialog.open) {
        dialog.showModal();
      }
      return;
    }
    var closer = event.target.closest ? event.target.closest("[data-modal-close]") : null;
    if (closer && closer.closest("dialog")) {
      closer.closest("dialog").close();
      return;
    }
    if (event.target.tagName === "DIALOG" && event.target.hasAttribute("data-backdrop-close")) {
      event.target.close();
    }
  });

  /* Links that start a slow redirect (e.g. Microsoft sign-in) show a busy state once clicked. */
  document.addEventListener("click", function (event) {
    var link = event.target.closest ? event.target.closest("[data-loading-link]") : null;
    if (link && !link.hasAttribute("aria-busy")) {
      link.setAttribute("aria-busy", "true");
    }
  });
  window.addEventListener("pageshow", function () {
    document.querySelectorAll("[data-loading-link][aria-busy]").forEach(function (link) {
      link.removeAttribute("aria-busy");
    });
  });

  /* The showcase form never posts: it only demonstrates the submitting state, then resets. */
  document.addEventListener("submit", function (event) {
    if (event.target.hasAttribute && event.target.hasAttribute("data-demo-form")) {
      event.preventDefault();
      if (window.cpToast) {
        window.cpToast("Demo form: nothing was saved.", "info");
      }
      window.setTimeout(function () {
        window.dispatchEvent(new Event("pageshow"));
      }, 1200);
    }
  });

  /* Showcase helper: buttons with data-demo-toast raise a sample toast. */
  document.addEventListener("click", function (event) {
    var button = event.target.closest ? event.target.closest("[data-demo-toast]") : null;
    if (button && window.cpToast) {
      var tone = button.getAttribute("data-demo-toast");
      window.cpToast("This is " + (tone === "error" ? "an error" : "a " + tone) + " notification.", tone);
    }
  });

  /* Tooltips: any element with data-tooltip="text" shows one on hover or keyboard focus (used
   * for the collapsed sidebar).  The bubble is positioned through the CSSOM (CSP-safe) and the
   * target is linked with aria-describedby while it is visible. */
  var tooltipNode = null;
  function showTooltip(target) {
    var text = target.getAttribute("data-tooltip");
    if (!text) {
      return;
    }
    var collapsedOnly = target.hasAttribute("data-tooltip-collapsed");
    if (collapsedOnly && document.documentElement.getAttribute("data-sidebar") !== "collapsed") {
      return;
    }
    if (!tooltipNode) {
      tooltipNode = document.createElement("div");
      tooltipNode.id = "cp-tooltip";
      tooltipNode.className = "tooltip";
      tooltipNode.setAttribute("role", "tooltip");
      document.body.appendChild(tooltipNode);
    }
    tooltipNode.textContent = text;
    tooltipNode.hidden = false;
    var rect = target.getBoundingClientRect();
    var side = target.getAttribute("data-tooltip-side") || "right";
    var top = side === "bottom" ? rect.bottom + 8 : rect.top + rect.height / 2 - tooltipNode.offsetHeight / 2;
    var left = side === "bottom" ? rect.left + rect.width / 2 - tooltipNode.offsetWidth / 2 : rect.right + 8;
    tooltipNode.style.top = Math.max(4, top) + "px";
    tooltipNode.style.left = Math.max(4, Math.min(left, window.innerWidth - tooltipNode.offsetWidth - 4)) + "px";
    target.setAttribute("aria-describedby", "cp-tooltip");
  }
  function hideTooltip(target) {
    if (tooltipNode) {
      tooltipNode.hidden = true;
    }
    if (target && target.removeAttribute) {
      target.removeAttribute("aria-describedby");
    }
  }
  ["mouseover", "focusin"].forEach(function (name) {
    document.addEventListener(name, function (event) {
      var target = event.target.closest ? event.target.closest("[data-tooltip]") : null;
      if (target) {
        showTooltip(target);
      }
    });
  });
  ["mouseout", "focusout"].forEach(function (name) {
    document.addEventListener(name, function (event) {
      var target = event.target.closest ? event.target.closest("[data-tooltip]") : null;
      if (target) {
        hideTooltip(target);
      }
    });
  });
  document.addEventListener("keydown", function (event) {
    if (event.key === "Escape") {
      hideTooltip(null);
    }
  });

  /* A lazily-loaded tree branch that fails to load says so (and can be retried by toggling). */
  document.addEventListener("htmx:responseError", function (event) {
    var target = event.detail && event.detail.target;
    if (target && target.classList && target.classList.contains("tree-children")) {
      target.textContent = "";
      var note = document.createElement("li");
      note.className = "px-2 py-1.5 text-sm text-danger-text";
      note.textContent = "These items could not be loaded. Close and reopen this branch to try again.";
      target.appendChild(note);
    } else if (window.cpToast && !(event.detail.elt && event.detail.elt.closest && event.detail.elt.closest("[data-htmx-scope]"))) {
      window.cpToast("That request could not be completed. Please try again.", "error");
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

  /* Offline workspace identity hints (Phase 10): two opaque ids in localStorage - no token, no data.  An authenticated page
   * records who is signed in; signing out marks the session ended so the offline workspace stops opening that account's data. */
  function initOfflineAccount() {
    try {
      var root = document.documentElement;
      var user = root.getAttribute("data-account-user");
      var org = root.getAttribute("data-account-org");
      if (user && org) {
        window.localStorage.setItem("cp-offline-account", JSON.stringify({ userId: user, organizationId: org }));
        window.localStorage.removeItem("cp-offline-session-ended");
      }
      document.addEventListener("submit", function (event) {
        var action = event.target && event.target.getAttribute && event.target.getAttribute("action");
        if (action && action.indexOf("/auth/logout") !== -1) {
          window.localStorage.setItem("cp-offline-session-ended", String(Date.now()));
        }
      });
    } catch (err) {
      /* storage unavailable: the offline workspace then asks for a fresh sign-in */
    }
  }

  /* Evidence upload progress (Phase 11): HTMX reports XHR progress; show it on the matching <progress>.  Purely presentational - the
   * server decides whether the file was accepted. */
  document.addEventListener("htmx:xhr:progress", function (event) {
    var form = event.target && event.target.closest ? event.target.closest("form[id^=ev-form-]") : null;
    var bar = form && document.getElementById("ev-progress-" + form.id.slice("ev-form-".length));
    if (bar && event.detail && event.detail.total) {
      bar.classList.remove("hidden");
      bar.value = Math.round((100 * event.detail.loaded) / event.detail.total);
    }
  });

  initOfflineAccount();
  initServiceWorker();
})();

