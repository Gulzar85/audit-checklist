"""Theme admin.

The admin is for *oversight and lifecycle*, not for hand-editing tokens: token values change in
the theme editor (colour pickers, bounded numbers, fixed choices) which validates everything.
A raw token/JSON box here would bypass that, so ``tokens`` is read-only (shown as a summary).
Every write - create, publish, activate, rollback, archive - goes through ``services.py``.
Themes are archived, never deleted.
"""

from __future__ import annotations

from django import forms
from django.contrib import admin, messages
from django.core.exceptions import ValidationError
from django.urls import reverse
from django.utils.html import format_html, format_html_join
from simple_history.admin import SimpleHistoryAdmin

from . import services
from .models import Theme, ThemeActivation, UserAppearance
from .policies import can_manage_theme
from .presets import preset_choices
from .selectors import themes_visible_to


class ThemeAdminForm(forms.ModelForm):
    """Creation form (an editable draft is created; tokens are edited in the editor)."""

    preset = forms.ChoiceField(
        choices=[("", "Blank (built-in defaults)"), *preset_choices()], required=False, label="Start from"
    )

    class Meta:
        model = Theme
        fields = ("organization", "name", "code", "description", "default_appearance")

    def __init__(self, *args, **kwargs) -> None:  # noqa: ANN002, ANN003
        super().__init__(*args, **kwargs)
        if "code" in self.fields:  # read-only (absent) once the theme exists
            self.fields["code"].required = False
            self.fields["code"].help_text = "Stable identifier. Derived from the name when left empty; cannot change later."

    def clean_organization(self):  # noqa: ANN201
        organization = self.cleaned_data.get("organization")
        if organization is not None and not organization.is_active:
            raise forms.ValidationError("This organization is not active.")
        return organization


@admin.register(Theme)
class ThemeAdmin(SimpleHistoryAdmin):
    form = ThemeAdminForm
    list_display = ("name", "code", "version", "scope", "organization", "status", "default_appearance", "updated_at")
    list_filter = ("status", "scope", "default_appearance", "organization")
    search_fields = ("name", "code", "organization__name")
    ordering = ("scope", "name", "-version")
    list_per_page = 50
    actions = ("publish_selected", "activate_selected", "rollback_selected", "archive_selected")
    readonly_fields = (
        "id", "scope", "status", "version", "source_preset", "palette", "editor_link",
        "published_at", "activated_at", "created_by", "updated_by", "created_at", "updated_at",
    )

    fieldsets = (
        (None, {"fields": ("organization", "name", "code", "description", "default_appearance")}),
        ("Lifecycle", {"fields": ("scope", "status", "version", "published_at", "activated_at"),
                       "description": "Use the actions on the list page (publish, activate, roll back, archive)."}),
        ("Design tokens", {"fields": ("palette", "editor_link", "source_preset"),
                           "description": "Tokens are edited in the theme editor, which validates every value."}),
        ("Audit", {"fields": ("id", "created_by", "updated_by", "created_at", "updated_at")}),
    )
    add_fieldsets = (
        (None, {"fields": ("organization", "name", "code", "preset", "description", "default_appearance")}),
    )

    # -- scoping & permissions --------------------------------------------------------------
    def get_queryset(self, request):  # noqa: ANN001, ANN201
        return themes_visible_to(request.user)

    def get_fieldsets(self, request, obj=None):  # noqa: ANN001, ANN201
        return self.add_fieldsets if obj is None else super().get_fieldsets(request, obj)

    def get_readonly_fields(self, request, obj=None):  # noqa: ANN001, ANN201
        fields = super().get_readonly_fields(request, obj)
        if obj is not None:
            # Identity is fixed at creation; only drafts may change their descriptive fields.
            fields = (*fields, "organization", "code")
            if not obj.is_editable:
                fields = (*fields, "name", "description", "default_appearance")
        return fields

    def has_delete_permission(self, request, obj=None) -> bool:  # noqa: ANN001
        return False

    def has_change_permission(self, request, obj=None) -> bool:  # noqa: ANN001
        allowed = super().has_change_permission(request, obj)
        return allowed and (obj is None or can_manage_theme(request.user, obj))

    # -- display ---------------------------------------------------------------------------------
    @admin.display(description="palette")
    def palette(self, obj: Theme):  # noqa: ANN201
        if obj is None or not obj.pk:
            return "—"
        from .resolution import effective_tokens_for

        tokens = effective_tokens_for(obj)
        keys = ("primary", "secondary", "accent", "background", "surface", "foreground", "success", "warning", "danger", "info")
        return format_html_join(
            " ",
            '<span title="{} {}" class="font-monospace">{} {}</span>',
            ((k, tokens["light"][k], k, tokens["light"][k]) for k in keys),
        )

    @admin.display(description="editor")
    def editor_link(self, obj: Theme):  # noqa: ANN201
        if obj is None or not obj.pk:
            return "—"
        name = "themes:edit" if obj.is_editable else "themes:detail"
        label = "Open the theme editor" if obj.is_editable else "View the theme"
        return format_html('<a href="{}">{}</a>', reverse(name, kwargs={"pk": obj.pk}), label)

    # -- writes go through the service layer -----------------------------------------------------------
    def save_model(self, request, obj, form, change) -> None:  # noqa: ANN001
        try:
            if change:
                saved = services.update_theme(
                    obj,
                    actor=request.user,
                    name=obj.name,
                    description=obj.description,
                    default_appearance=obj.default_appearance,
                )
            else:
                saved = services.create_theme(
                    actor=request.user,
                    organization=obj.organization,
                    name=obj.name,
                    code=obj.code,
                    description=obj.description,
                    preset=form.cleaned_data.get("preset", ""),
                    default_appearance=obj.default_appearance,
                )
        except ValidationError as exc:
            for message in exc.messages:
                messages.error(request, message)
            return
        obj.__dict__.update(saved.__dict__)

    def _each(self, request, queryset, operation, label: str) -> None:  # noqa: ANN001
        done = 0
        for theme in queryset:
            try:
                operation(theme, actor=request.user)
                done += 1
            except ValidationError as exc:
                self.message_user(request, f"{theme}: {' '.join(exc.messages)}", messages.ERROR)
        if done:
            self.message_user(request, f"{done} theme(s) {label}.", messages.SUCCESS)

    @admin.action(description="Publish selected themes")
    def publish_selected(self, request, queryset):  # noqa: ANN001, ANN201
        self._each(request, queryset, services.publish_theme, "published")

    @admin.action(description="Activate selected themes")
    def activate_selected(self, request, queryset):  # noqa: ANN001, ANN201
        self._each(request, queryset, services.activate_theme, "activated")

    @admin.action(description="Roll back (selected themes must be active)")
    def rollback_selected(self, request, queryset):  # noqa: ANN001, ANN201
        self._each(request, queryset, services.rollback_theme, "rolled back")

    @admin.action(description="Archive selected themes")
    def archive_selected(self, request, queryset):  # noqa: ANN001, ANN201
        self._each(request, queryset, services.archive_theme, "archived")


@admin.register(ThemeActivation)
class ThemeActivationAdmin(admin.ModelAdmin):
    """Read-only ledger of what was active when."""

    list_display = ("theme", "organization", "via", "activated_by", "activated_at", "deactivated_at")
    list_filter = ("via", "organization")
    date_hierarchy = "activated_at"
    search_fields = ("theme__name", "theme__code")

    def has_add_permission(self, request) -> bool:  # noqa: ANN001
        return False

    def has_change_permission(self, request, obj=None) -> bool:  # noqa: ANN001
        return False

    def has_delete_permission(self, request, obj=None) -> bool:  # noqa: ANN001
        return False


@admin.register(UserAppearance)
class UserAppearanceAdmin(admin.ModelAdmin):
    list_display = ("user", "mode", "updated_at")
    list_filter = ("mode",)
    search_fields = ("user__email",)
    readonly_fields = ("user", "mode", "created_at", "updated_at")

    def has_add_permission(self, request) -> bool:  # noqa: ANN001
        return False

    def has_delete_permission(self, request, obj=None) -> bool:  # noqa: ANN001
        return False
