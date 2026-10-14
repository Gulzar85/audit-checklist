/* Checklist Platform - application shell behaviour.
 * No inline scripts are used; this file is self-hosted and covered by CSP. */
(function () {
  "use strict";

  var THEME_KEY = "cp-theme";

  function getStoredTheme() {
    try {
      return window.localStorage.getItem(THEME_KEY);
    } catch (err) {
      return null;
    }
  }

  function storeTheme(theme) {
    try {
      window.localStorage.setItem(THEME_KEY, theme);
    } catch (err) {
      /* storage may be unavailable (private mode) - ignore */
    }
  }

  function applyTheme(theme) {
    var root = document.documentElement;
    if (theme === "dark") {
      root.classList.add("dark");
    } else if (theme === "light") {
      root.classList.remove("dark");
    } else {
      var prefersDark = window.matchMedia("(prefers-color-scheme: dark)").matches;
      root.classList.toggle("dark", prefersDark);
    }
    var meta = document.querySelector('meta[name="color-scheme"]');
    if (meta) {
      meta.setAttribute("content", theme === "dark" ? "dark" : "light dark");
    }
  }

  function initTheme() {
    var stored = getStoredTheme();
    applyTheme(stored || "system");
    window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", function (event) {
      if (!getStoredTheme()) {
        document.documentElement.classList.toggle("dark", event.matches);
      }
    });
  }

  function initThemeToggle() {
    document.querySelectorAll("[data-theme-toggle]").forEach(function (button) {
      button.addEventListener("click", function () {
        var isDark = document.documentElement.classList.contains("dark");
        var next = isDark ? "light" : "dark";
        storeTheme(next);
        applyTheme(next);
      });
    });
  }

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

  document.addEventListener("DOMContentLoaded", function () {
    initTheme();
    initThemeToggle();
    initIcons();
  });

  // Re-render icons after HTMX swaps content in.
  document.body && document.body.addEventListener("htmx:afterSwap", initIcons);

  initServiceWorker();
})();
