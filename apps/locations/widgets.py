"""Form widgets for the location domain."""

from __future__ import annotations

import json
from typing import Any

from django import forms
from django.urls import reverse

from apps.core.utils import parse_uuid


class LocationPickerWidget(forms.Widget):
    """A searchable location selector (combobox) backed by an HTMX endpoint.

    A ``<select>`` listing every location would be unusable (and slow) for an
    organization with thousands of sites.  This widget renders a search box that
    queries ``locations:picker`` as the user types and stores the chosen
    location's id in a hidden input.  The server still validates the submitted
    id against the field's scoped queryset, so the widget is a convenience, never
    the security boundary.

    ``organization_source`` is a CSS selector of the organization field whose
    current value narrows the search; ``organization_id`` pins it (edit forms,
    where the organization is fixed) and ``exclude_id`` omits a location and its
    subtree (a location cannot be moved beneath itself).
    """

    template_name = "locations/widgets/location_picker.html"
    manages_own_classes = True

    def __init__(
        self,
        attrs: dict[str, Any] | None = None,
        *,
        include_inactive: bool = False,
        organization_source: str = "",
        require_organization: bool = False,
    ) -> None:
        super().__init__(attrs)
        self.include_inactive = include_inactive
        self.organization_source = organization_source
        self.require_organization = require_organization
        self.organization_id: Any = None
        self.exclude_id: Any = None
        #: When set, shown instead of the selected location's name (a parent outside the user's scope).
        self.hide_label: str = ""
        #: Set by ``ModelChoiceField`` (``field.queryset = ...``); gives the widget
        #: the field's scoped queryset for label look-ups.
        self.choices: Any = None

    def _selected_label(self, value: Any) -> str:
        """The chosen location's label, looked up **only within the field's own queryset**.

        ``ModelChoiceField`` hands its (already scoped) queryset to the widget via
        ``choices``.  Using it - rather than ``Location.objects`` - means a submitted
        id that is outside the user's scope renders an empty box, never the name
        or code of another tenant's location.
        """
        if self.hide_label and value:
            return self.hide_label
        pk = parse_uuid(value)
        queryset = getattr(self.choices, "queryset", None)
        if pk is None or queryset is None:
            return ""
        row = queryset.filter(pk=pk).values_list("name", "code").first()
        return f"{row[0]} [{row[1]}]" if row else ""

    def get_context(self, name: str, value: Any, attrs: dict[str, Any] | None) -> dict[str, Any]:
        context = super().get_context(name, value, attrs)
        widget = context["widget"]
        static_params: dict[str, str] = {}
        if self.include_inactive:
            static_params["include_inactive"] = "1"
        if self.require_organization:
            static_params["require_organization"] = "1"
        if self.organization_id:
            static_params["organization"] = str(self.organization_id)
        if self.exclude_id:
            static_params["exclude"] = str(self.exclude_id)

        widget.update(
            {
                "search_url": reverse("locations:picker"),
                "organization_source": self.organization_source if not self.organization_id else "",
                "static_params": json.dumps(static_params) if static_params else "",
                "selected_label": self._selected_label(value),
                "aria_invalid": widget["attrs"].get("aria-invalid", ""),
                "aria_describedby": widget["attrs"].get("aria-describedby", ""),
            }
        )
        return context
