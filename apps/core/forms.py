"""Shared form helpers.

Forms are rendered with crispy-forms + crispy-tailwind, whose field templates are
overridden in ``templates/tailwind/`` to use the platform design system (so they
share dark mode, focus rings and spacing with the rest of the UI).  These mixins
supply the matching widget classes and accessibility attributes.
"""

from __future__ import annotations

from typing import Any

from django import forms
from django.core.exceptions import ValidationError
from django.utils.html import format_html, format_html_join
from django_filters.widgets import BooleanWidget


class DesignSystemFormMixin:
    """Give every widget the design-system CSS class and accessible error state.

    * text-like inputs get ``input``; selects ``select``; textareas ``textarea``;
      checkboxes ``checkbox``; file inputs ``file-input``;
    * a field with errors gets ``aria-invalid="true"`` and is linked to its error
      list (``aria-describedby``), so assistive technology announces the problem.
    """

    _WIDGET_CLASSES: tuple[tuple[type, str], ...] = (
        (forms.CheckboxInput, "checkbox"),
        (forms.RadioSelect, ""),
        (forms.CheckboxSelectMultiple, ""),
        (forms.Select, "select"),
        (forms.Textarea, "textarea"),
        (forms.FileInput, "file-input"),
        (forms.HiddenInput, ""),
        (forms.widgets.Input, "input"),
    )

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        for field in self.fields.values():  # type: ignore[attr-defined]
            widget = field.widget
            if getattr(widget, "manages_own_classes", False):
                continue
            for widget_type, css in self._WIDGET_CLASSES:
                if isinstance(widget, widget_type):
                    if css:
                        existing = widget.attrs.get("class", "")
                        if css not in existing.split():
                            widget.attrs["class"] = f"{existing} {css}".strip()
                    break

    def full_clean(self) -> None:
        super().full_clean()  # type: ignore[misc]
        for name in self.errors:  # type: ignore[attr-defined]
            field = self.fields.get(name)  # type: ignore[attr-defined]
            if field is None:
                continue
            describedby = f"error_{self[name].auto_id}"  # type: ignore[index]
            field.widget.attrs["aria-invalid"] = "true"
            field.widget.attrs["aria-describedby"] = describedby


class DesignSystemFilterForm(DesignSystemFormMixin, forms.Form):
    """Base form for django-filter filtersets (``Meta.form``) so filter inputs share the design system."""


class YesNoAnyWidget(BooleanWidget):
    """A tri-state filter select: Any / Yes / No (django-filter's default says "Unknown")."""

    def __init__(self, attrs: dict[str, Any] | None = None) -> None:
        super().__init__(attrs)
        self.choices = (("", "Any"), ("true", "Yes"), ("false", "No"))


class SectionedFormMixin:
    """Group fields into titled sections for consistent, scannable layouts.

    Declare ``sections = (("Title", ("field", ...)), ...)``; templates iterate
    ``form.get_sections``.  Fields not mentioned are still rendered, in a final
    untitled section, so a field can never silently disappear from the page.
    """

    sections: tuple[tuple[str, tuple[str, ...]], ...] = ()

    @property
    def get_sections(self) -> list[dict[str, Any]]:
        placed: set[str] = set()
        result: list[dict[str, Any]] = []
        for title, names in self.sections:
            bound = [self[name] for name in names if name in self.fields]  # type: ignore[attr-defined,index]
            placed.update(names)
            if bound:
                result.append({"title": title, "fields": bound})
        leftovers = [
            self[name]  # type: ignore[index]
            for name in self.fields  # type: ignore[attr-defined]
            if name not in placed and not self.fields[name].widget.is_hidden  # type: ignore[attr-defined]
        ]
        if leftovers:
            result.append({"title": "", "fields": leftovers})
        return result


class DatalistInput(forms.TextInput):
    """A text input with browser-native suggestions (``<datalist>``).

    Suited to long, well-known lists (time zones, languages) where a ``<select>``
    with hundreds of options would be unwieldy but free typing should still be
    validated server-side.
    """

    def __init__(self, options: Any = (), attrs: dict[str, Any] | None = None) -> None:
        super().__init__(attrs)
        self.options = options

    def render(self, name, value, attrs=None, renderer=None):  # noqa: ANN001, ANN201
        attrs = {**(attrs or {})}
        list_id = f"{attrs.get('id') or f'id_{name}'}_options"
        attrs["list"] = list_id
        attrs.setdefault("autocomplete", "off")
        html = super().render(name, value, attrs, renderer)
        options = self.options() if callable(self.options) else self.options
        datalist = format_html(
            '<datalist id="{}">{}</datalist>',
            list_id,
            format_html_join("", '<option value="{}">', ((o,) for o in options)),
        )
        return html + datalist


def add_validation_error(form: forms.BaseForm, error: ValidationError) -> None:
    """Attach a service-layer ``ValidationError`` to ``form``.

    Field-keyed errors land on the matching field (so the user sees them beside
    the input); anything else becomes a non-field error.
    """
    if hasattr(error, "error_dict"):
        for field, errors in error.error_dict.items():
            target = field if field in form.fields else None
            for item in errors:
                form.add_error(target, item)
    else:
        for item in error.error_list:
            form.add_error(None, item)
