/* Checklist Platform - admin compatibility shim.
 *
 * Jazzmin 3.0.x loads `jazzmin/js/change_form.js` on django-import-export's import
 * screen, but that screen never loads Django's `admin/js/jquery.init.js`, so the
 * final line of change_form.js (`django.jQuery(document).on("formset:added", ...)`)
 * throws "django is not defined".  Jazzmin runs `custom_js` just before that script,
 * so exposing the already-loaded jQuery under Django's namespace is enough.
 *
 * Harmless where Django has already defined it; remove when Jazzmin fixes the import
 * template.  Self-hosted, so it is covered by the strict CSP.
 */
(function (window) {
  "use strict";
  if (window.jQuery) {
    window.django = window.django || {};
    window.django.jQuery = window.django.jQuery || window.jQuery;
  }
})(window);
