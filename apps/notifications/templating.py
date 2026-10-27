"""Safe notification templates.

A template may contain **only** ``{{ variable }}`` placeholders whose name is one of the event's allowed variables (``registry.Event.variables``).  No tags, no
filters, no attribute access, no comments: anything else is a validation error before a template can be activated.  Rendering uses a Django template ``Engine``
with *no* builtins and *no* libraries and a context of plain strings only, so even a mistake in validation could not reach a model, a callable or a setting.
HTML bodies are rendered with auto-escaping; text, subject and in-app copy are rendered as text (the in-app message is escaped again when displayed).  A subject can
never contain a newline (header injection).  Templates never receive answers, evidence, tokens or addresses - only the minimal variables of ``registry``.
"""

from __future__ import annotations

import re
from typing import Any

from django.template import Context, Engine

_VARIABLE = re.compile(r"\{\{(.*?)\}\}", re.S)
_NAME = re.compile(r"^[a-z][a-z0-9_]{0,40}$")
_FORBIDDEN = re.compile(r"\{%|%\}|\{#|#\}")

LIMITS = {"subject": 200, "body_text": 5000, "body_html": 10000, "in_app_title": 160, "in_app_message": 500}
FIELDS = tuple(LIMITS)

_TEXT = Engine(autoescape=False, builtins=[], libraries={})
_HTML = Engine(autoescape=True, builtins=[], libraries={})


def variables_of(text: str) -> list[str]:
    return [m.strip() for m in _VARIABLE.findall(text or "")]


def validate_field(name: str, text: str, allowed: tuple[str, ...]) -> list[str]:
    """Problems with one template field (empty = fine)."""
    problems: list[str] = []
    if len(text or "") > LIMITS[name]:
        problems.append(f"{name}: at most {LIMITS[name]} characters.")
    if _FORBIDDEN.search(text or ""):
        problems.append(f"{name}: template tags and comments are not allowed; use {{{{ variable }}}} placeholders only.")
        return problems
    stripped = _VARIABLE.sub("", text or "")
    if "{{" in stripped or "}}" in stripped:
        problems.append(f"{name}: unbalanced {{{{ }}}} placeholder.")
    for raw in variables_of(text):
        if not _NAME.match(raw):
            problems.append(f"{name}: “{{{{ {raw} }}}}” is not a plain variable (no filters, attributes or expressions).")
        elif raw not in allowed:
            problems.append(f"{name}: the variable “{raw}” is not available for this event (use: {', '.join(allowed)}).")
    return problems


def validate(fields: dict[str, str], allowed: tuple[str, ...]) -> list[str]:
    """All problems of a template; ``fields`` maps the five field names to their text."""
    problems: list[str] = []
    for name in FIELDS:
        problems.extend(validate_field(name, fields.get(name, ""), allowed))
    email = bool(fields.get("subject", "").strip() and fields.get("body_text", "").strip())
    in_app = bool(fields.get("in_app_title", "").strip() and fields.get("in_app_message", "").strip())
    if not (email or in_app):
        problems.append("A template needs a subject and a text body (email) or an in-app title and message (in-app) - or both.")
    if fields.get("body_html", "").strip() and not fields.get("body_text", "").strip():
        problems.append("body_html: an HTML body needs the plain-text body as well (the plain-text fallback).")
    return problems


def _render(engine: Engine, text: str, context: dict[str, str], *, escape: bool) -> str:
    """``escape`` is set on the ``Context`` itself (a ``Context`` ignores the engine's own autoescape setting)."""
    return engine.from_string(text or "").render(Context({k: str(v) for k, v in context.items()}, autoescape=escape))


def render(fields: dict[str, str], context: dict[str, str]) -> dict[str, str]:
    """Render every non-empty field with ``context`` (plain strings).  Raises nothing for valid templates; callers validate first."""
    out = {"subject": "", "body_text": "", "body_html": "", "in_app_title": "", "in_app_message": ""}
    if fields.get("subject"):
        out["subject"] = " ".join(_render(_TEXT, fields["subject"], context, escape=False).split())[: LIMITS["subject"]]
    if fields.get("body_text"):
        out["body_text"] = _render(_TEXT, fields["body_text"], context, escape=False)[: LIMITS["body_text"]]
    if fields.get("body_html"):
        out["body_html"] = _render(_HTML, fields["body_html"], context, escape=True)[: LIMITS["body_html"] * 2]
    if fields.get("in_app_title"):
        out["in_app_title"] = " ".join(_render(_TEXT, fields["in_app_title"], context, escape=False).split())[: LIMITS["in_app_title"]]
    if fields.get("in_app_message"):
        out["in_app_message"] = " ".join(_render(_TEXT, fields["in_app_message"], context, escape=False).split())[: LIMITS["in_app_message"]]
    return out


def sample_context(allowed: tuple[str, ...]) -> dict[str, str]:
    """Synthetic values for previews - never real data."""
    samples = {"recipient_name": "Alex Sample", "organization_name": "Sample Organization", "reference": "REF-00001", "title": "Sample title", "location_name": "Sample Location",
               "actor_name": "Jordan Example", "link": "https://app.example.com/sample/", "event_label": "Sample event", "due_date": "01 Jan 2030 09:00", "priority": "High",
               "severity": "High", "category": "Safety", "status": "Open", "outcome": "Review required", "step": "Manager approval", "channel": "Email", "error": "provider unavailable"}
    return {name: samples.get(name, f"<{name}>") for name in allowed}


def as_dict(obj: Any) -> dict[str, str]:
    return {name: getattr(obj, name, "") or "" for name in FIELDS}
