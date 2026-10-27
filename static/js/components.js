/* Checklist Platform - Alpine.js components.
 *
 * Alpine is the CSP build (ADR 0004), which cannot evaluate inline expressions:
 * no object literals in x-data, no `open = true`, no `n++`, no `!flag`, and no
 * x-model.  Every piece of client state therefore lives in a named component
 * registered here, and templates refer to properties, getters and methods by
 * plain name (`x-show="open"`, `@click="toggle"`).
 *
 * This file must load *before* alpine.min.js so the `alpine:init` listener is
 * registered in time (see templates/base.html).
 */
/* Appearance (light / dark / system).  The mode is the `data-theme` attribute on <html>; the
 * theme stylesheet (/themes/tokens.css) holds light, dark and system rules for it, so
 * switching is instant and needs no reload.  The server renders the right value on first
 * paint from the user's saved choice or the `cp_appearance` cookie - this code only handles
 * *changes*.  No inline script is involved (strict CSP). */
window.cpAppearance = (function () {
  "use strict";

  var COOKIE = "cp_appearance";
  var MODES = ["light", "dark", "system"];

  function applyMetas(mode) {
    var media = {
      light: { light: "all", dark: "not all" },
      dark: { light: "not all", dark: "all" },
      system: { light: "(prefers-color-scheme: light)", dark: "(prefers-color-scheme: dark)" },
    }[mode];
    document.querySelectorAll('meta[name="theme-color"][data-scheme]').forEach(function (meta) {
      meta.setAttribute("media", media[meta.getAttribute("data-scheme")]);
    });
    var scheme = document.querySelector('meta[name="color-scheme"]');
    if (scheme) {
      scheme.setAttribute("content", mode === "system" ? "light dark" : mode);
    }
  }

  function apply(mode) {
    if (MODES.indexOf(mode) === -1) {
      return;
    }
    document.documentElement.setAttribute("data-theme", mode);
    applyMetas(mode);
    var secure = window.location.protocol === "https:" ? "; Secure" : "";
    document.cookie = COOKIE + "=" + mode + "; path=/; max-age=31536000; SameSite=Lax" + secure;
  }

  function csrfToken() {
    try {
      return JSON.parse(document.body.getAttribute("hx-headers") || "{}")["X-CSRFToken"] || "";
    } catch (err) {
      return "";
    }
  }

  function persist(url, mode) {
    window
      .fetch(url, {
        method: "POST",
        credentials: "same-origin",
        headers: {
          "Content-Type": "application/x-www-form-urlencoded",
          "X-CSRFToken": csrfToken(),
        },
        body: "mode=" + encodeURIComponent(mode),
      })
      .catch(function () {
        /* the choice is already applied locally; saving can fail silently */
      });
  }

  return { apply: apply, persist: persist };
})();

/* Toasts (global helpers).  Messages are inserted with textContent - never as HTML. */
window.cpScheduleToast = function (item) {
  "use strict";
  var close = item.querySelector("[data-toast-close]");
  if (close && !close.getAttribute("data-bound")) {
    close.setAttribute("data-bound", "true");
    close.addEventListener("click", function () {
      item.remove();
    });
  }
  if (item.getAttribute("data-tone") !== "error") {
    window.setTimeout(function () {
      item.remove();
    }, 7000);
  }
};

window.cpToast = function (message, tone) {
  "use strict";
  var region = document.querySelector("[data-toast-region]");
  if (!region || !message) {
    return;
  }
  var tones = { success: "circle-check", info: "info", warning: "triangle-alert", error: "circle-alert" };
  var kind = tones[tone] ? tone : "info";
  var item = document.createElement("div");
  item.className = "toast toast-" + kind;
  item.setAttribute("data-toast", "");
  item.setAttribute("data-tone", kind);
  item.setAttribute("role", kind === "error" ? "alert" : "status");
  var icon = document.createElement("i");
  icon.setAttribute("data-lucide", tones[kind]);
  icon.setAttribute("class", "h-5 w-5 shrink-0");
  icon.setAttribute("aria-hidden", "true");
  var text = document.createElement("span");
  text.className = "flex-1";
  text.textContent = message;
  var button = document.createElement("button");
  button.type = "button";
  button.className = "btn btn-ghost btn-icon btn-sm -my-1 shrink-0";
  button.setAttribute("aria-label", "Dismiss notification");
  button.setAttribute("data-toast-close", "");
  var x = document.createElement("i");
  x.setAttribute("data-lucide", "x");
  x.setAttribute("class", "h-4 w-4");
  x.setAttribute("aria-hidden", "true");
  button.appendChild(x);
  item.appendChild(icon);
  item.appendChild(text);
  item.appendChild(button);
  region.appendChild(item);
  if (window.lucide && typeof window.lucide.createIcons === "function") {
    window.lucide.createIcons();
  }
  window.cpScheduleToast(item);
};
document.addEventListener("alpine:init", function () {
  "use strict";

  /* The navbar's appearance menu: choose light, dark or system. */
  Alpine.data("appearance", function () {
    return {
      open: false,
      mode: "system",
      init: function () {
        this.mode = this.$root.dataset.current || "system";
      },
      get isLight() {
        return this.mode === "light";
      },
      get isDark() {
        return this.mode === "dark";
      },
      get isSystem() {
        return this.mode === "system";
      },
      toggle: function () {
        this.open = !this.open;
      },
      close: function () {
        this.open = false;
      },
      choose: function (mode) {
        this.mode = mode;
        this.open = false;
        window.cpAppearance.apply(mode);
        var url = this.$root.dataset.persistUrl;
        if (url) {
          window.cpAppearance.persist(url, mode);
        }
      },
      setLight: function () {
        this.choose("light");
      },
      setDark: function () {
        this.choose("dark");
      },
      setSystem: function () {
        this.choose("system");
      },
    };
  });

  /* Live theme preview.  The editor form posts to the server on every change (HTMX); the
   * server validates the tokens and answers with a dictionary of CSS custom properties
   * (`data-preview-vars`).  This component only *applies* that dictionary to the preview
   * frame through the CSSOM (allowed by the CSP, unlike an inline style).  All token logic
   * stays on the server. */
  Alpine.data("themePreview", function () {
    return {
      applied: [],
      init: function () {
        var self = this;
        document.addEventListener("htmx:afterSwap", function (event) {
          if (event.target && event.target.id === "preview-feedback") {
            self.sync();
          }
        });
        this.sync();
      },
      sync: function () {
        var frame = this.$root.querySelector("[data-preview-frame]");
        var holder = this.$root.querySelector("[data-preview-vars]");
        if (!frame || !holder) {
          return;
        }
        var vars;
        try {
          vars = JSON.parse(holder.getAttribute("data-preview-vars"));
        } catch (err) {
          return;
        }
        this.applied.forEach(function (name) {
          frame.style.removeProperty(name);
        });
        this.applied = [];
        Object.keys(vars).forEach(function (name) {
          if (/^--[a-z0-9-]+$/.test(name) && typeof vars[name] === "string") {
            frame.style.setProperty(name, vars[name]);
            this.applied.push(name);
          }
        }, this);
        frame.setAttribute(
          "data-theme",
          holder.getAttribute("data-preview-mode") === "dark" ? "dark" : "light"
        );
      },
    };
  });

  /* A simple show/hide region (mobile navigation, collapsible panels). */
  Alpine.data("disclosure", function () {
    return {
      open: false,
      // Optional toggle text: data-open-label / data-closed-label on the root element.
      get label() {
        var data = this.$root.dataset;
        return this.open ? data.openLabel || "Hide" : data.closedLabel || "Show";
      },
      toggle: function () {
        this.open = !this.open;
      },
      close: function () {
        this.open = false;
      },
    };
  });

  /* The filter form above a list/tree.  On phones it starts collapsed behind a
   * "Show filters" button so the data is not pushed off-screen; from the md
   * breakpoint up it is open (and the button is hidden by CSS).  The form is hidden
   * with a bound `hidden` attribute (not x-show) so that if this component is missing
   * or fails, the form simply stays visible - it must fail open. */
  Alpine.data("filterPanel", function () {
    return {
      open: true,
      init: function () {
        this.open = window.matchMedia("(min-width: 768px)").matches;
      },
      get collapsed() {
        return !this.open;
      },
      get label() {
        return this.open ? "Hide filters" : "Show filters";
      },
      toggle: function () {
        this.open = !this.open;
      },
    };
  });

  /* A modal confirmation built on the native <dialog> element, which supplies
   * focus trapping, Escape-to-close and the backdrop.  Markup:
   *   <div x-data="confirmDialog">
   *     <button type="button" @click="show">...</button>
   *     <dialog x-ref="dialog" @click="onBackdrop" @close="onClose"> ... </dialog>
   *   </div>
   * `cascade` mirrors an optional "include descendants" checkbox so the dialog
   * can reveal an extra warning (see locations/location_detail.html). */
  /* Searchable checkbox list of locations (bulk assignment).  CSP-safe: methods only, no inline expressions.
   * Rows are `[data-loc-row]` (with `data-text` = lower-case path) and checkboxes carry `data-loc`. */
  Alpine.data("locationPicker", function () {
    return {
      selected: 0,
      init: function () {
        this.recount();
      },
      boxes: function (onlyVisible) {
        var out = [];
        this.$root.querySelectorAll("[data-loc-row]").forEach(function (row) {
          if (onlyVisible && row.hidden) {
            return;
          }
          var box = row.querySelector("input[data-loc]");
          if (box) {
            out.push(box);
          }
        });
        return out;
      },
      recount: function () {
        this.selected = this.boxes(false).filter(function (box) {
          return box.checked;
        }).length;
      },
      filter: function (event) {
        var query = (event.target.value || "").toLowerCase();
        this.$root.querySelectorAll("[data-loc-row]").forEach(function (row) {
          row.hidden = query !== "" && row.getAttribute("data-text").indexOf(query) === -1;
        });
      },
      all: function () {
        this.boxes(true).forEach(function (box) {
          box.checked = true;
        });
        this.recount();
      },
      none: function () {
        this.boxes(false).forEach(function (box) {
          box.checked = false;
        });
        this.recount();
      },
    };
  });

  Alpine.data("confirmDialog", function () {
    return {
      cascade: false,
      // When the root element has data-require-cascade="true", the confirm button
      // (:disabled="blocked") stays disabled until the cascade box is ticked.
      get blocked() {
        return this.$root.getAttribute("data-require-cascade") === "true" && !this.cascade;
      },
      show: function () {
        this.$refs.dialog.showModal();
      },
      hide: function () {
        this.$refs.dialog.close();
      },
      onBackdrop: function (event) {
        // A click on the <dialog> element itself (not its content) is the backdrop.
        if (event.target === this.$refs.dialog) {
          this.$refs.dialog.close();
        }
      },
      onClose: function () {
        this.cascade = false;
      },
      onCascade: function (event) {
        this.cascade = event.target.checked;
      },
    };
  });

  /* Expand / collapse every node of the location tree.  The nodes themselves are
   * native <details> elements, so keyboard and screen-reader support is built in. */
  Alpine.data("treeControls", function () {
    return {
      expandAll: function () {
        this.$root.querySelectorAll("details").forEach(function (node) {
          node.open = true;
        });
      },
      collapseAll: function () {
        this.$root.querySelectorAll("details").forEach(function (node) {
          node.open = false;
        });
      },
    };
  });

  /* ==========================================================================
   * Application shell (Phase 0.5 design system).  All state is Alpine; markup lives in
   * templates/layouts/app.html and templates/components/**.  No inline expressions.
   * ========================================================================== */

  function focusable(root, selector) {
    return Array.prototype.slice.call(root.querySelectorAll(selector)).filter(function (node) {
      return !node.disabled && node.offsetParent !== null;
    });
  }

  /* The shell: mobile drawer + desktop collapse (persisted in a cookie the server reads) and a
   * quiet session watcher that warns before the session ends. */
  Alpine.data("shell", function () {
    return {
      mobileOpen: false,
      collapsed: false,
      watcher: null,
      init: function () {
        var self = this;
        this.collapsed = document.documentElement.getAttribute("data-sidebar") === "collapsed";
        window.matchMedia("(min-width: 1024px)").addEventListener("change", function (event) {
          if (event.matches) {
            self.closeMobile();
          }
        });
        var url = this.$root.getAttribute("data-session-url");
        if (url) {
          this.watcher = window.setInterval(function () {
            self.checkSession(url);
          }, 60000);
        }
      },
      get drawerState() {
        return this.mobileOpen ? "true" : "false";
      },
      get expanded() {
        return !this.collapsed;
      },
      get collapseLabel() {
        return this.collapsed ? "Expand sidebar" : "Collapse sidebar";
      },
      toggleMobile: function () {
        this.mobileOpen = !this.mobileOpen;
        document.body.classList.toggle("overflow-hidden", this.mobileOpen);
        if (this.mobileOpen) {
          var self = this;
          this.$nextTick(function () {
            var links = focusable(self.$refs.sidebar, "a[href], button");
            if (links.length) {
              links[0].focus();
            }
          });
        }
      },
      closeMobile: function () {
        if (this.mobileOpen) {
          this.mobileOpen = false;
          document.body.classList.remove("overflow-hidden");
        }
      },
      toggleCollapse: function () {
        this.collapsed = !this.collapsed;
        var state = this.collapsed ? "collapsed" : "expanded";
        document.documentElement.setAttribute("data-sidebar", state);
        var secure = window.location.protocol === "https:" ? "; Secure" : "";
        document.cookie = "cp_sidebar=" + state + "; path=/; max-age=31536000; SameSite=Lax" + secure;
      },
      checkSession: function (url) {
        if (document.hidden) {
          return;
        }
        window
          .fetch(url, { credentials: "same-origin", headers: { Accept: "application/json" } })
          .then(function (response) {
            return response.json();
          })
          .then(function (state) {
            if (!state.authenticated) {
              window.cpToast("Your session has ended. Sign in again to continue.", "error");
            } else if (state.expires_in !== null && state.expires_in < 120) {
              window.cpToast("Your session will expire soon. Save your work.", "warning");
            }
          })
          .catch(function () {
            /* offline or transient: the network indicator covers it */
          });
      },
    };
  });

  /* A dropdown / menu button with arrow-key navigation.  Items carry role="menuitem". */
  Alpine.data("dropdown", function () {
    return {
      open: false,
      items: function () {
        return focusable(this.$root, '[role="menuitem"], [role="menuitemradio"]');
      },
      toggle: function () {
        this.open = !this.open;
        if (this.open) {
          var self = this;
          this.$nextTick(function () {
            var items = self.items();
            if (items.length) {
              items[0].focus();
            }
          });
        }
      },
      close: function () {
        this.open = false;
      },
      closeAndFocus: function () {
        if (this.open) {
          this.open = false;
          var trigger = this.$root.querySelector("[aria-haspopup]");
          if (trigger) {
            trigger.focus();
          }
        }
      },
      onKeydown: function (event) {
        if (!this.open) {
          return;
        }
        var items = this.items();
        var index = items.indexOf(document.activeElement);
        if (event.key === "ArrowDown") {
          event.preventDefault();
          items[(index + 1) % items.length].focus();
        } else if (event.key === "ArrowUp") {
          event.preventDefault();
          items[(index - 1 + items.length) % items.length].focus();
        } else if (event.key === "Home") {
          event.preventDefault();
          items[0].focus();
        } else if (event.key === "End") {
          event.preventDefault();
          items[items.length - 1].focus();
        } else if (event.key === "Tab") {
          this.open = false;
        }
      },
    };
  });

  /* In-page tabs (WAI-ARIA tabs pattern).  Panels carry role="tabpanel"; without script every
   * panel simply stays visible.  The active tab is mirrored in the URL hash (#tab-<key>). */
  /* Schedule form: shows only the recurrence fields of the selected schedule type (no inline expressions: CSP build). */
  Alpine.data("scheduleForm", function () {
    return {
      mode: "",
      init: function () {
        var select = this.$root.querySelector('[name="schedule_type"]');
        this.mode = select ? select.value : "";
      },
      sync: function (event) {
        this.mode = event.target.value;
      },
      get isOnce() { return this.mode === "one_time"; },
      get isRecurring() { return this.mode !== "one_time" && this.mode !== ""; },
      get isInterval() { return this.mode === "interval"; },
      get hasTime() { return this.mode === "daily" || this.mode === "weekly" || this.mode === "monthly"; },
      get isWeekly() { return this.mode === "weekly"; },
      get isMonthly() { return this.mode === "monthly"; }
    };
  });

  Alpine.data("tabs", function () {
    return {
      active: "",
      buttons: function () {
        return Array.prototype.slice.call(this.$root.querySelectorAll('[role="tab"]'));
      },
      init: function () {
        var buttons = this.buttons();
        if (!buttons.length) {
          return;
        }
        var wanted = (window.location.hash || "").replace("#tab-", "");
        var known = buttons.map(function (button) {
          return button.getAttribute("data-tab");
        });
        this.activate(known.indexOf(wanted) !== -1 ? wanted : known[0], false);
      },
      select: function (event) {
        this.activate(event.currentTarget.getAttribute("data-tab"), true);
      },
      activate: function (key, updateHash) {
        this.active = key;
        this.buttons().forEach(function (button) {
          var selected = button.getAttribute("data-tab") === key;
          button.setAttribute("aria-selected", selected ? "true" : "false");
          button.setAttribute("tabindex", selected ? "0" : "-1");
        });
        this.$root.querySelectorAll('[role="tabpanel"]').forEach(function (panel) {
          panel.hidden = panel.getAttribute("data-tab-panel") !== key;
        });
        if (updateHash && window.history && window.history.replaceState) {
          window.history.replaceState(null, "", "#tab-" + key);
        }
      },
      onKeydown: function (event) {
        var buttons = this.buttons();
        var index = buttons.indexOf(document.activeElement);
        if (index === -1) {
          return;
        }
        var next = null;
        if (event.key === "ArrowRight") {
          next = buttons[(index + 1) % buttons.length];
        } else if (event.key === "ArrowLeft") {
          next = buttons[(index - 1 + buttons.length) % buttons.length];
        } else if (event.key === "Home") {
          next = buttons[0];
        } else if (event.key === "End") {
          next = buttons[buttons.length - 1];
        }
        if (next) {
          event.preventDefault();
          next.focus();
          this.activate(next.getAttribute("data-tab"), true);
        }
      },
    };
  });

  /* Server-rendered Django messages become toasts; each can be dismissed, success/info/warning
   * also dismiss themselves (errors stay until closed).  Toasts raised from script go through
   * window.cpToast (below), which is also what HX-Trigger "toast" events call. */
  Alpine.data("toasts", function () {
    return {
      init: function () {
        var region = this.$root;
        region.querySelectorAll("[data-toast]").forEach(function (item) {
          window.cpScheduleToast(item);
        });
        document.addEventListener("toast", function (event) {
          var detail = event.detail || {};
          window.cpToast(detail.message || detail.value || "", detail.tone || "info");
        });
      },
      dismiss: function (event) {
        var item = event.currentTarget.closest("[data-toast]");
        if (item) {
          item.remove();
        }
      },
    };
  });

  /* The command palette (global search UI).  Ctrl/Cmd+K or "/" opens it; the input is an HTMX
   * request to the search endpoint; results are server-rendered, grouped links. */
  Alpine.data("commandPalette", function () {
    return {
      hasQuery: false,
      init: function () {
        var self = this;
        document.addEventListener("keydown", function (event) {
          var typing = /^(INPUT|TEXTAREA|SELECT)$/.test((event.target || {}).tagName || "") || (event.target || {}).isContentEditable;
          if ((event.key === "k" && (event.ctrlKey || event.metaKey)) || (event.key === "/" && !typing && !event.ctrlKey && !event.metaKey)) {
            event.preventDefault();
            self.show();
          }
        });
      },
      show: function () {
        if (!this.$refs.dialog.open) {
          this.$refs.dialog.showModal();
        }
        this.$refs.input.focus();
        this.$refs.input.select();
      },
      hide: function () {
        this.$refs.dialog.close();
      },
      onBackdrop: function (event) {
        if (event.target === this.$refs.dialog) {
          this.$refs.dialog.close();
        }
      },
      onInput: function () {
        this.hasQuery = this.$refs.input.value.length > 0;
      },
      clear: function () {
        this.$refs.input.value = "";
        this.hasQuery = false;
        this.$refs.input.dispatchEvent(new Event("input", { bubbles: true }));
        this.$refs.input.focus();
      },
      onKeydown: function (event) {
        if (event.key !== "ArrowDown" && event.key !== "ArrowUp") {
          return;
        }
        var stops = [this.$refs.input].concat(focusable(this.$refs.results, "a[href]"));
        var index = stops.indexOf(document.activeElement);
        var step = event.key === "ArrowDown" ? 1 : -1;
        event.preventDefault();
        stops[(index + step + stops.length) % stops.length].focus();
      },
    };
  });

  /* Online / offline indicator (ready for the offline checklist phase; no sync logic here). */
  Alpine.data("networkStatus", function () {
    return {
      online: true,
      init: function () {
        var self = this;
        this.online = window.navigator.onLine;
        window.addEventListener("online", function () {
          self.online = true;
        });
        window.addEventListener("offline", function () {
          self.online = false;
        });
      },
      get offline() {
        return !this.online;
      },
      get label() {
        return this.online ? "Online" : "Offline";
      },
      get stateClass() {
        return this.online ? "net-online" : "net-offline";
      },
    };
  });

  /* Form chrome for ordinary (non-HTMX) forms: disables submit while submitting, and warns
   * before leaving a form with unsaved changes. */
  Alpine.data("formGuard", function () {
    return {
      submitting: false,
      dirty: false,
      markDirty: function () {
        this.dirty = true;
      },
      onSubmit: function (event) {
        if (this.submitting) {
          event.preventDefault();
          return;
        }
        this.submitting = true;
        this.dirty = false;
      },
      reset: function () {
        this.submitting = false;
      },
      warn: function (event) {
        if (this.dirty && !this.submitting) {
          event.preventDefault();
          event.returnValue = "";
        }
      },
    };
  });

  /* Arrow-key navigation for tree views (the nodes themselves are native <details>). */
  Alpine.data("treeNav", function () {
    return {
      stops: function () {
        return focusable(this.$root, "summary, a[data-tree-leaf]");
      },
      onKeydown: function (event) {
        var stops = this.stops();
        var index = stops.indexOf(document.activeElement);
        if (index === -1 && event.target && event.target.closest) {
          index = stops.indexOf(event.target.closest("summary"));
        }
        if (index === -1) {
          return;
        }
        var current = stops[index];
        var details = current.tagName === "SUMMARY" ? current.parentElement : null;
        if (event.key === "ArrowDown") {
          event.preventDefault();
          stops[Math.min(index + 1, stops.length - 1)].focus();
        } else if (event.key === "ArrowUp") {
          event.preventDefault();
          stops[Math.max(index - 1, 0)].focus();
        } else if (event.key === "ArrowRight" && details && !details.open) {
          event.preventDefault();
          details.open = true;
        } else if (event.key === "ArrowLeft") {
          if (details && details.open) {
            event.preventDefault();
            details.open = false;
          } else {
            var parent = current.closest("details") && current.closest("details").parentElement.closest("details");
            if (parent) {
              event.preventDefault();
              parent.querySelector("summary").focus();
            }
          }
        } else if (event.key === "Home") {
          event.preventDefault();
          stops[0].focus();
        } else if (event.key === "End") {
          event.preventDefault();
          stops[stops.length - 1].focus();
        }
      },
    };
  });
  /* Searchable single-select (combobox) for locations.  The visible text input
   * queries an HTMX endpoint; choosing a result stores its id in the hidden
   * input.  The server re-validates the id against a scoped queryset - this
   * widget is a convenience, not a security boundary. */
  Alpine.data("picker", function () {
    return {
      open: false,
      active: -1,
      show: function () {
        this.open = true;
      },
      hide: function () {
        this.open = false;
        this.active = -1;
      },
      options: function () {
        return Array.prototype.slice.call(this.$refs.results.querySelectorAll("[data-value]"));
      },
      onInput: function () {
        // Typing invalidates a previous choice until a new result is picked.
        this.$refs.value.value = "";
        this.open = true;
        this.active = -1;
      },
      choose: function (event) {
        var option = event.target.closest("[data-value]");
        if (!option) {
          return;
        }
        this.$refs.value.value = option.getAttribute("data-value");
        this.$refs.input.value = option.getAttribute("data-label");
        this.$refs.value.dispatchEvent(new Event("change", { bubbles: true }));
        this.hide();
      },
      clear: function () {
        this.$refs.value.value = "";
        this.$refs.input.value = "";
        this.$refs.value.dispatchEvent(new Event("change", { bubbles: true }));
        this.$refs.input.focus();
        this.hide();
      },
      onKeydown: function (event) {
        var options = this.options();
        if (event.key === "ArrowDown" || event.key === "ArrowUp") {
          event.preventDefault();
          this.open = true;
          if (!options.length) {
            return;
          }
          var step = event.key === "ArrowDown" ? 1 : -1;
          this.active = (this.active + step + options.length) % options.length;
          options.forEach(function (node, index) {
            node.setAttribute("aria-selected", index === this.active ? "true" : "false");
          }, this);
          options[this.active].scrollIntoView({ block: "nearest" });
        } else if (event.key === "Enter" && this.open && this.active >= 0) {
          event.preventDefault();
          options[this.active].click();
        } else if (event.key === "Escape") {
          this.hide();
        }
      },
    };
  });
  /* Permission matrix (role editor).  Filters rows by text, and the "all" box of a row ticks or
   * clears every enabled permission in it.  Rows and boxes are server-rendered; this only
   * toggles visibility and `checked`, so the form works without it. */
  Alpine.data("permissionMatrix", function () {
    return {
      empty: false,
      onFilter: function (event) {
        var query = (event.target.value || "").trim().toLowerCase();
        var visible = 0;
        this.$root.querySelectorAll("[data-matrix-row]").forEach(function (row) {
          var match = !query || (row.getAttribute("data-search") || "").indexOf(query) !== -1;
          row.hidden = !match;
          if (match) {
            visible += 1;
          }
        });
        this.empty = visible === 0;
      },
      toggleRow: function (event) {
        var row = event.target.closest("[data-matrix-row]");
        if (!row) {
          return;
        }
        var checked = event.target.checked;
        row.querySelectorAll('input[name="permissions"]:not(:disabled)').forEach(function (box) {
          box.checked = checked;
        });
      },
      syncRow: function (event) {
        var row = event.target.closest("[data-matrix-row]");
        var all = row && row.querySelector("[data-row-all]");
        if (!all || event.target === all) {
          return;
        }
        var boxes = row.querySelectorAll('input[name="permissions"]');
        var ticked = row.querySelectorAll('input[name="permissions"]:checked');
        all.checked = boxes.length > 0 && boxes.length === ticked.length;
      },
    };
  });

  /* Role assignment: the location field only applies to the location scopes. */
  Alpine.data("scopeFields", function () {
    return {
      needsLocation: false,
      init: function () {
        this.onScope();
      },
      onScope: function () {
        var select = this.$root.querySelector("[data-scope-type]");
        this.needsLocation = !!select && select.value !== "organization";
      },
    };
  });

  /* Checklist execution workspace: a truthful save indicator around the HTMX autosave.  The server renders "Saved HH:MM:SS"
     after it has stored the answers; this only reports an in-flight request or a failure - it never claims a save itself. */
  Alpine.data("executionForm", function () {
    function status(text, bad) {
      var el = document.getElementById("exec-status");
      if (!el) {
        return;
      }
      el.textContent = text;
      el.classList.toggle("text-danger-text", !!bad);
    }
    function isForm(event) {
      return !!(event.detail && event.detail.elt && event.detail.elt.id === "exec-form");
    }
    return {
      init: function () {
        var root = this.$el;
        root.addEventListener("htmx:beforeRequest", function (event) {
          if (isForm(event)) {
            status("Saving…", false);
          }
        });
        root.addEventListener("htmx:beforeSwap", function (event) {
          var url = (event.detail.xhr && event.detail.xhr.responseURL) || "";
          if (isForm(event) && url.indexOf("/auth/login") !== -1) {
            event.detail.shouldSwap = false;  /* the session expired: never replace the form with the sign-in page */
            status("Your session expired. Your answers on this page were not saved - sign in again, then re-enter them.", true);
          }
        });
        var failed = function (event) {
          if (isForm(event)) {
            status("Not saved. Your answers are still on this page - check your connection and try again.", true);
          }
        };
        root.addEventListener("htmx:responseError", failed);
        root.addEventListener("htmx:sendError", failed);
      },
    };
  });
});
