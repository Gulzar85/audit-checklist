"""Class-based views for the theme engine.

Same contract as the rest of the platform: reads are scoped through
:func:`~apps.themes.selectors.themes_visible_to` (out of scope = 404), authorization asks
``policies.py`` (visible but forbidden = 403) and every write goes through ``services.py``.
"""

from __future__ import annotations

import json
from typing import Any

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import PermissionDenied, ValidationError
from django.http import HttpRequest, HttpResponse, HttpResponseRedirect, JsonResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme
from django.views import View
from django.views.generic import DetailView, FormView, TemplateView
from django.views.generic.detail import SingleObjectMixin
from django_filters.views import FilterView
from django_tables2 import SingleTableMixin

from apps.core.forms import add_validation_error
from apps.core.mixins import PolicyMixin
from apps.core.policies import is_active_user

from . import contrast, services
from . import tokens as tk
from .constants import (
    APPEARANCE_COOKIE,
    APPEARANCE_COOKIE_MAX_AGE,
    PREVIEW_SESSION_KEY,
    Appearance,
)
from .filters import ThemeFilter
from .forms import (
    AppearanceForm,
    ThemeCloneForm,
    ThemeCreateForm,
    ThemeEditForm,
    ThemeImportForm,
)
from .models import Theme
from .policies import (
    can_activate_theme,
    can_archive_theme,
    can_clone_theme,
    can_create_theme,
    can_edit_theme,
    can_export_theme,
    can_import_theme,
    can_manage_theme,
    can_preview_theme,
    can_publish_theme,
    can_rollback_theme,
)
from .resolution import effective_tokens_for, resolve_theme, stylesheet_for
from .selectors import activation_history, theme_history, themes_visible_to
from .tables import ThemeTable


def _flash_validation_error(request: HttpRequest, error: ValidationError) -> None:
    for message in error.messages:
        messages.error(request, message)


class ThemeScopedMixin:
    """Fetch themes only from those the current user may see."""

    def get_queryset(self):  # noqa: ANN201
        return themes_visible_to(self.request.user)


# --- the public pieces: stylesheet and appearance -------------------------------------------------
class ThemeStylesheetView(View):
    """``/themes/tokens.css`` - the active theme as CSS custom properties.

    Public (anonymous visitors get the system theme) but *personalised by request*: the same URL
    serves a different organization's tokens to its members, so it is ``private`` and validated
    with an ETag (a 304 costs a couple of small queries, no CSS work).
    """

    def get(self, request: HttpRequest) -> HttpResponse:
        resolved = resolve_theme(request)
        etag = f'"{resolved.version}"'
        if request.headers.get("If-None-Match") == etag:
            response = HttpResponse(status=304)
        else:
            response = HttpResponse(stylesheet_for(resolved), content_type="text/css; charset=utf-8")
        response["ETag"] = etag
        response["Cache-Control"] = "private, no-cache"
        response["Vary"] = "Cookie"
        return response


class AppearanceView(View):
    """POST: remember the user's light / dark / system choice (cookie always; server when signed in)."""

    def post(self, request: HttpRequest) -> HttpResponse:
        form = AppearanceForm(request.POST)
        if not form.is_valid():
            return HttpResponse(status=400)
        mode = form.cleaned_data["mode"]
        if is_active_user(request.user):
            try:
                services.set_user_appearance(request.user, mode or None)
            except ValidationError:
                return HttpResponse(status=400)
        response = HttpResponse(status=204)
        if mode in Appearance.values:
            response.set_cookie(
                APPEARANCE_COOKIE,
                mode,
                max_age=APPEARANCE_COOKIE_MAX_AGE,
                samesite="Lax",
                secure=request.is_secure(),
                httponly=False,  # the appearance menu keeps it in sync client-side
            )
        else:
            response.delete_cookie(APPEARANCE_COOKIE)
        return response


# --- list / detail ---------------------------------------------------------------------------------
class ThemeListView(LoginRequiredMixin, SingleTableMixin, FilterView):
    table_class = ThemeTable
    filterset_class = ThemeFilter
    template_name = "themes/theme_list.html"
    context_object_name = "themes"
    table_pagination = {"per_page": 20}

    def get_queryset(self):  # noqa: ANN201
        return themes_visible_to(self.request.user)

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        user = self.request.user
        context["can_create"] = user.has_perm("themes.add_theme")
        context["resolved"] = resolve_theme(self.request)
        context["hidden_filters"] = ()
        return context


class ThemeDetailView(ThemeScopedMixin, LoginRequiredMixin, DetailView):
    template_name = "themes/theme_detail.html"
    context_object_name = "theme"

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        theme: Theme = self.object
        user = self.request.user
        effective = effective_tokens_for(theme)
        checks = contrast.contrast_checks(effective)
        context.update(
            can_edit=can_edit_theme(user, theme),
            can_publish=can_publish_theme(user, theme),
            can_activate=can_activate_theme(user, theme),
            can_rollback=can_rollback_theme(user, theme),
            can_archive=can_archive_theme(user, theme),
            can_clone=can_clone_theme(user, theme),
            can_new_version=can_manage_theme(user, theme) and not theme.is_draft,
            can_preview=can_preview_theme(user, theme),
            can_export=can_export_theme(user, theme),
            checks=checks,
            blocking=[c for c in checks if c.level == "error"],
            warnings=[c for c in checks if c.level == "warning"],
            swatches=_swatches(effective),
            history=theme_history(theme, limit=15) if can_manage_theme(user, theme) else [],
            activations=activation_history(theme, limit=10),
            versions=themes_visible_to(user).filter(organization=theme.organization, code=theme.code).order_by("-version"),
        )
        return context


def _swatches(tokens: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    keys = ("primary", "secondary", "accent", "background", "surface", "foreground", "success", "warning", "danger", "info")
    return [
        {"key": key, "label": tk.TOKENS[key].label, "light": tokens["light"][key], "dark": tokens["dark"][key]}
        for key in keys
    ]


# --- create / edit -----------------------------------------------------------------------------------
class ThemeCreateView(PolicyMixin, FormView):
    form_class = ThemeCreateForm
    template_name = "themes/theme_create.html"

    def policy_allows(self, obj: Any = None) -> bool:  # noqa: ANN401
        return self.request.user.has_perm("themes.add_theme")

    def get_form_kwargs(self) -> dict[str, Any]:
        return {**super().get_form_kwargs(), "user": self.request.user}

    def form_valid(self, form: ThemeCreateForm) -> HttpResponse:
        data = form.cleaned_data
        try:
            theme = services.create_theme(
                actor=self.request.user,
                organization=data["organization"],
                name=data["name"],
                description=data["description"],
                preset=data["preset"],
                default_appearance=data["default_appearance"],
            )
        except ValidationError as exc:
            add_validation_error(form, exc)
            return self.form_invalid(form)
        messages.success(self.request, f"Draft “{theme.name}” created. Customize it below.")
        return redirect("themes:edit", pk=theme.pk)


class ThemeEditView(ThemeScopedMixin, PolicyMixin, SingleObjectMixin, FormView):
    """The editor.  Drafts only; published themes are immutable (create a new version)."""

    template_name = "themes/theme_edit.html"
    policy_uses_object = True

    def policy_allows(self, obj: Any = None) -> bool:  # noqa: ANN401
        return can_manage_theme(self.request.user, obj)

    def get_form_class(self):  # noqa: ANN201
        return ThemeEditForm

    def get_form_kwargs(self) -> dict[str, Any]:
        return {**super().get_form_kwargs(), "theme": self.object}

    def get(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        self.object = self.get_object()
        if not self.object.is_editable:
            messages.info(request, "Published themes are immutable. Create a new version to change it.")
            return redirect(self.object)
        return super().get(request, *args, **kwargs)

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        self.object = self.get_object()
        if not self.object.is_editable:
            messages.error(request, "Published themes are immutable. Create a new version to change it.")
            return redirect(self.object)
        return super().post(request, *args, **kwargs)

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        form: ThemeEditForm = context["form"]
        tokens = form.effective_tokens()
        mode = form.preview_selected_mode
        context.update(
            theme=self.object,
            preview=_preview_context(form, tokens, mode),
            preview_mode=mode,
        )
        return context

    def form_valid(self, form: ThemeEditForm) -> HttpResponse:
        data = form.cleaned_data
        try:
            theme = services.update_theme(
                self.object,
                actor=self.request.user,
                name=data["name"],
                description=data["description"],
                default_appearance=data["default_appearance"],
                tokens=form.token_overrides(),
            )
        except ValidationError as exc:
            add_validation_error(form, exc)
            return self.form_invalid(form)
        messages.success(self.request, f"Theme “{theme.name}” saved.")
        return redirect(theme)


def _preview_context(form: ThemeEditForm, tokens: dict[str, dict[str, Any]], mode: str) -> dict[str, Any]:
    checks = contrast.contrast_checks(tokens)
    return {
        "variables_json": json.dumps(tk.preview_variables(tokens, mode)),
        "mode": mode,
        "checks": checks,
        "blocking": [c for c in checks if c.level == "error"],
        "warnings": [c for c in checks if c.level == "warning"],
        "errors": [
            f"{form.fields[name].label}: {' '.join(messages_)}"
            for name, messages_ in form.errors.items()
            if name in form.fields
        ],
    }


class ThemePreviewFragmentView(ThemeScopedMixin, PolicyMixin, SingleObjectMixin, View):
    """HTMX: validate the *unsaved* editor form and return the CSS variables to preview.

    Nothing is stored.  The reply is a small fragment: a ``data-preview-vars`` dictionary (applied
    to the preview frame by the ``themePreview`` component) plus the live accessibility report.
    """

    policy_uses_object = True

    def policy_allows(self, obj: Any = None) -> bool:  # noqa: ANN401
        return can_manage_theme(self.request.user, obj) and obj.is_editable

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        theme = self.get_object()
        form = ThemeEditForm(request.POST, theme=theme)
        form.is_valid()
        return render(
            request,
            "themes/_preview_feedback.html",
            {"preview": _preview_context(form, form.effective_tokens(), form.preview_selected_mode), "theme": theme},
        )


# --- lifecycle actions (POST only) --------------------------------------------------------------------
class ThemeActionView(ThemeScopedMixin, PolicyMixin, SingleObjectMixin, View):
    """Base for one-click lifecycle operations: policy -> service -> flash message -> back to detail."""

    policy_uses_object = True
    success_message = ""

    def policy_check(self, theme: Theme) -> bool:  # pragma: no cover - interface
        raise NotImplementedError

    def perform(self, theme: Theme) -> Theme:  # pragma: no cover - interface
        raise NotImplementedError

    def policy_allows(self, obj: Any = None) -> bool:  # noqa: ANN401
        return self.policy_check(obj)

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        theme = self.get_object()
        try:
            result = self.perform(theme)
        except ValidationError as exc:
            _flash_validation_error(request, exc)
            return redirect(theme)
        messages.success(request, self.success_message.format(name=result.name, version=result.version))
        return redirect(result)


class ThemePublishView(ThemeActionView):
    success_message = "Theme “{name}” v{version} published. It can now be activated."

    def policy_check(self, theme: Theme) -> bool:
        return can_publish_theme(self.request.user, theme)

    def perform(self, theme: Theme) -> Theme:
        return services.publish_theme(theme, actor=self.request.user)


class ThemeActivateView(ThemeActionView):
    success_message = "Theme “{name}” v{version} is now active."

    def policy_check(self, theme: Theme) -> bool:
        return can_activate_theme(self.request.user, theme)

    def perform(self, theme: Theme) -> Theme:
        return services.activate_theme(theme, actor=self.request.user)


class ThemeRollbackView(ThemeActionView):
    success_message = "Rolled back: “{name}” v{version} is active again."

    def policy_check(self, theme: Theme) -> bool:
        return can_rollback_theme(self.request.user, theme)

    def perform(self, theme: Theme) -> Theme:
        return services.rollback_theme(theme, actor=self.request.user)


class ThemeArchiveView(ThemeActionView):
    success_message = "Theme “{name}” v{version} archived."

    def policy_check(self, theme: Theme) -> bool:
        return can_archive_theme(self.request.user, theme)

    def perform(self, theme: Theme) -> Theme:
        return services.archive_theme(theme, actor=self.request.user)


class ThemeNewVersionView(ThemeActionView):
    success_message = "Draft version {version} of “{name}” created."

    def policy_check(self, theme: Theme) -> bool:
        return can_manage_theme(self.request.user, theme) and not theme.is_draft

    def perform(self, theme: Theme) -> Theme:
        return services.create_new_version(theme, actor=self.request.user)


class ThemeCloneView(ThemeScopedMixin, PolicyMixin, SingleObjectMixin, FormView):
    form_class = ThemeCloneForm
    template_name = "themes/theme_clone.html"
    policy_uses_object = True

    def policy_allows(self, obj: Any = None) -> bool:  # noqa: ANN401
        return can_clone_theme(self.request.user, obj)

    def get(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        self.object = self.get_object()
        return super().get(request, *args, **kwargs)

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        self.object = self.get_object()
        return super().post(request, *args, **kwargs)

    def get_form_kwargs(self) -> dict[str, Any]:
        return {**super().get_form_kwargs(), "user": self.request.user, "theme": self.object}

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        return {**super().get_context_data(**kwargs), "theme": self.object}

    def form_valid(self, form: ThemeCloneForm) -> HttpResponse:
        try:
            clone = services.clone_theme(
                self.object,
                actor=self.request.user,
                name=form.cleaned_data["name"],
                organization=form.cleaned_data["organization"],
            )
        except ValidationError as exc:
            add_validation_error(form, exc)
            return self.form_invalid(form)
        messages.success(self.request, f"Cloned as “{clone.name}” (draft).")
        return redirect("themes:edit", pk=clone.pk)


# --- export / import --------------------------------------------------------------------------------------
class ThemeExportView(ThemeScopedMixin, PolicyMixin, SingleObjectMixin, View):
    policy_uses_object = True

    def policy_allows(self, obj: Any = None) -> bool:  # noqa: ANN401
        return can_export_theme(self.request.user, obj)

    def get(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        theme = self.get_object()
        response = JsonResponse(services.export_theme(theme), json_dumps_params={"indent": 2})
        response["Content-Disposition"] = f'attachment; filename="theme-{theme.code.lower()}-v{theme.version}.json"'
        return response


class ThemeImportView(PolicyMixin, FormView):
    form_class = ThemeImportForm
    template_name = "themes/theme_import.html"

    def policy_allows(self, obj: Any = None) -> bool:  # noqa: ANN401
        return self.request.user.has_perm("themes.add_theme")

    def get_form_kwargs(self) -> dict[str, Any]:
        return {**super().get_form_kwargs(), "user": self.request.user}

    def form_valid(self, form: ThemeImportForm) -> HttpResponse:
        organization = form.cleaned_data["organization"]
        if not can_import_theme(self.request.user, organization):  # defence in depth
            raise PermissionDenied
        try:
            document = services.parse_import_file(form.cleaned_data["file"].read(65 * 1024))
            theme = services.import_theme(document, actor=self.request.user, organization=organization)
        except ValidationError as exc:
            add_validation_error(form, exc)
            return self.form_invalid(form)
        messages.success(self.request, f"Imported “{theme.name}” as a draft.")
        return redirect("themes:edit", pk=theme.pk)


# --- temporary, per-session preview ---------------------------------------------------------------------------
class ThemeTryView(ThemeScopedMixin, PolicyMixin, SingleObjectMixin, View):
    """Preview a theme across the whole application - for this session only."""

    policy_uses_object = True

    def policy_allows(self, obj: Any = None) -> bool:  # noqa: ANN401
        return can_preview_theme(self.request.user, obj)

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        theme = self.get_object()
        request.session[PREVIEW_SESSION_KEY] = str(theme.pk)
        return HttpResponseRedirect(reverse("core:home"))


class ThemePreviewStopView(LoginRequiredMixin, View):
    def post(self, request: HttpRequest) -> HttpResponse:
        request.session.pop(PREVIEW_SESSION_KEY, None)
        target = request.POST.get("next") or reverse("themes:list")
        if not url_has_allowed_host_and_scheme(target, {request.get_host()}, request.is_secure()):
            target = reverse("themes:list")
        return HttpResponseRedirect(target)


class ThemeGuideView(LoginRequiredMixin, TemplateView):
    """A live gallery of the real components (also the visual-regression target)."""

    template_name = "themes/theme_gallery.html"
