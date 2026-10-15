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
});
