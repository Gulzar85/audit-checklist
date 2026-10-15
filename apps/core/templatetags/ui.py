"""Template tags for the design system (``{% load ui %}``).

Components are Django template partials under ``templates/components/``; these tags are the
ergonomic way to use them so pages never copy component HTML.

* ``{% icon "name" size="sm" %}`` - a Lucide icon, always ``aria-hidden`` (decorative).
* ``{% button "Save" variant="primary" icon="save" type="submit" %}`` - one button hierarchy.
* ``{% status_badge value %}`` - semantic status -> tone + icon + label (never colour alone).
* ``{% card title="..." icon="..." %}...{% endcard %}`` - block components (card, modal, drawer,
  page header) render their body through the same partials.
* ``{% breadcrumbs "Home" "core:home" "Organizations" "organizations:list" "Acme" %}``.

Everything is auto-escaped; no tag accepts or emits unescaped user HTML.
"""

from __future__ import annotations

from typing import Any

from django import template
from django.template.loader import render_to_string
from django.urls import NoReverseMatch, reverse
from django.utils.html import format_html
from django.utils.safestring import SafeString

from apps.core.ui import ICON_SIZES, TONES, status_info

register = template.Library()


def _render(name: str, context: dict[str, Any]) -> SafeString:
    return render_to_string(name, context)  # type: ignore[return-value]


# --- atoms --------------------------------------------------------------------------------------
@register.simple_tag
def icon(name: str, size: str = "sm", css: str = "") -> SafeString:
    """A decorative Lucide icon (consistent sizes: xs/sm/md/lg/xl)."""
    classes = f"{ICON_SIZES.get(size, ICON_SIZES['sm'])} {css}".strip()
    return format_html('<i data-lucide="{}" class="{}" aria-hidden="true"></i>', name, classes)


@register.inclusion_tag("components/buttons/button.html")
def button(label: str = "", **options: Any) -> dict[str, Any]:
    """A button or link-button.  ``variant``: primary | secondary | outline | ghost | danger | link.

    Options: ``href`` (renders ``<a>``), ``icon``, ``size`` (sm|lg), ``type``, ``disabled``,
    ``icon_only`` (label becomes the accessible name), ``css`` (layout classes only),
    ``loading`` (shows the spinner affordance), ``attrs`` is intentionally not supported.
    """
    variant = options.pop("variant", "secondary")
    return {"label": label, "variant": variant, "type": options.pop("type", "button"), **options}


@register.inclusion_tag("components/badges/status.html")
def status_badge(value: object, label: str = "") -> dict[str, Any]:
    info = status_info(value)
    return {"tone": info.tone, "icon": info.icon, "label": label or info.label}


@register.simple_tag
def tone_badge(label: str, tone: str = "neutral", icon: str = "") -> SafeString:
    """A badge with an explicit semantic tone (use ``status_badge`` for known statuses)."""
    tone = tone if tone in TONES else "neutral"
    return _render("components/badges/status.html", {"tone": tone, "icon": icon, "label": label})


@register.filter
def status_tone(value: object) -> str:
    return status_info(value).tone


# --- block components ----------------------------------------------------------------------------
@register.simple_block_tag
def card(content: str, title: str = "", subtitle: str = "", icon: str = "", tone: str = "", css: str = "", flush: bool = False, heading_level: int = 2) -> SafeString:
    """A surface with an optional titled header.  ``flush`` removes body padding (tables/lists)."""
    return _render("components/cards/card.html", {
        "body": content, "title": title, "subtitle": subtitle, "icon": icon,
        "tone": tone if tone in TONES else "", "css": css, "flush": flush, "level": heading_level,
    })


@register.simple_block_tag
def page_header(content: str, title: str, subtitle: str = "", icon: str = "", status: str = "", status_label: str = "", image: Any = None, image_alt: str = "") -> SafeString:
    """The page title block.  The tag body holds the actions (primary action last).

    ``image`` may be an image field (an organization logo): when it holds a file it replaces the icon.
    """
    info = status_info(status) if status else None
    image_url = image.url if image and getattr(image, "name", "") else ""
    return _render("components/navigation/page_header.html", {
        "actions": content, "title": title, "subtitle": subtitle, "icon": icon, "image_url": image_url, "image_alt": image_alt,
        "status": status, "status_label": status_label or (info.label if info else ""),
    })


@register.simple_block_tag
def modal(content: str, id: str, title: str, size: str = "md", description: str = "") -> SafeString:  # noqa: A002
    """A native ``<dialog>`` modal.  Open it with ``data-modal-open="<id>"`` on any button."""
    return _render("components/modals/modal.html", {"body": content, "id": id, "title": title, "size": size, "description": description})


@register.simple_block_tag
def drawer(content: str, id: str, title: str, side: str = "right") -> SafeString:  # noqa: A002
    """A side drawer built on ``<dialog>`` (same open/close contract as ``modal``)."""
    return _render("components/modals/drawer.html", {"body": content, "id": id, "title": title, "side": side})


# --- breadcrumbs ------------------------------------------------------------------------------
@register.simple_tag
def breadcrumbs(*parts: str) -> SafeString:
    """Alternating ``label, target`` pairs; a trailing label without a target is the current page.

    ``target`` is a URL name (reversed) or a path starting with ``/``.  A pair whose target is
    empty or does not reverse is rendered as plain text rather than a broken link.
    """
    trail: list[dict[str, str | None]] = []
    items: list[Any] = []
    for part in parts:  # lists (from ``as_crumbs``) are spliced in
        items.extend(part if isinstance(part, (list, tuple)) else [part])
    while items:
        label = str(items.pop(0))
        target = items.pop(0) if items else None
        url: str | None = None
        if target:
            target = str(target)
            if target.startswith("/"):
                url = target
            else:
                try:
                    url = reverse(target)
                except NoReverseMatch:
                    url = None
        trail.append({"label": label, "url": url})
    return _render("components/navigation/breadcrumbs.html", {"trail": trail})

@register.filter
def as_crumbs(objects: Any, url_name: str) -> list[str]:
    """Flatten objects with ``name``/``pk`` into ``label, url`` pairs for ``{% breadcrumbs %}``."""
    flat: list[str] = []
    for obj in objects or []:
        flat += [str(getattr(obj, "name", obj)), reverse(url_name, args=[obj.pk])]
    return flat
