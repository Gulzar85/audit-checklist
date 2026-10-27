"""Notification rules and templates: validated, audited configuration.

* **Rules** (``NotificationRule``) are live configuration edited in place (every change bumps ``revision`` and is kept by ``django-simple-history``); a *disabled* rule is a
  draft.  An event type, strategies, channels, filters and the template code are validated against the fixed registries; making a rule **mandatory** needs the separate
  ``notification.mandatory`` permission.  A built-in default rule per event is created lazily per organization (``ensure_default_rule``): it can be edited or disabled, never deleted.
* **Templates** (``NotificationTemplate``) have a controlled lifecycle: *draft* (editable) -> *active* (immutable; one active version per code and organization) -> *archived*.
  Editing an active template creates the next draft version; activating validates every field and archives the previous active version.  A rule refers to a template *code*;
  with no usable active template the event's built-in default text is used (a documented fallback), so a missing or broken template never loses a notification.
"""

from __future__ import annotations

from typing import Any

from django.core.exceptions import PermissionDenied
from django.db import IntegrityError, transaction
from django.db.models import Max

from apps.core.services import stamp_history

from .. import constants as k
from .. import filters, policies, recipients, registry, templating
from ..models import NotificationRule, NotificationTemplate
from .base import VERBS, audit
from .errors import NotificationConflict, NotificationError

RULE_FIELDS = ("name", "description", "event_type", "enabled", "precedence", "recipient_strategies", "channels", "template_code", "priority", "preference_policy", "filters", "exclude_actor")


# ---------------------------------------------------------------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------------------------------------------------------------
def default_rule_code(event_code: str) -> str:
    return "default-" + event_code.replace(".", "-").replace("_", "-")


def ensure_default_rule(organization: Any, event: registry.Event) -> NotificationRule:
    """The organization's default rule for ``event`` (created once; never recreated after an edit, never deleted)."""
    code = default_rule_code(event.code)
    rule = NotificationRule.objects.filter(organization=organization, code=code).first()
    if rule is not None:
        return rule
    d = event.default_rule
    try:
        with transaction.atomic():
            return NotificationRule.objects.create(
                organization=organization, code=code, name=event.label, description=event.description, event_type=event.code, enabled=True, is_system_default=True,
                precedence=1000, recipient_strategies=[dict(s) for s in d.strategies], channels=list(d.channels), priority=d.priority, preference_policy=d.policy,
                filters=[dict(f) for f in d.filters], exclude_actor=d.exclude_actor)
    except IntegrityError:
        return NotificationRule.objects.get(organization=organization, code=code)


def seed_defaults(organization: Any) -> int:
    """Create the missing default rules of ``organization`` (idempotent).  Returns how many were created."""
    before = NotificationRule.objects.filter(organization=organization).count()
    for event in registry.EVENTS.values():
        ensure_default_rule(organization, event)
    return NotificationRule.objects.filter(organization=organization).count() - before


# ---------------------------------------------------------------------------------------------------------------------------------
# Rules
# ---------------------------------------------------------------------------------------------------------------------------------
def clean_rule(organization: Any, data: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Validate a rule payload.  Returns the normalized field values and the problems."""
    problems: list[str] = []
    out: dict[str, Any] = {}
    event = registry.get_event(str(data.get("event_type", "")))
    if event is None:
        problems.append(f"event_type must be one of: {', '.join(registry.EVENTS)}.")
    name = str(data.get("name", "")).strip()
    if not name or len(name) > 120:
        problems.append("name: 1 to 120 characters are required.")
    description = str(data.get("description", "")).strip()
    if len(description) > 500:
        problems.append("description: at most 500 characters.")
    enabled = data.get("enabled", True)
    exclude_actor = data.get("exclude_actor", True)
    for label, value in (("enabled", enabled), ("exclude_actor", exclude_actor)):
        if not isinstance(value, bool):
            problems.append(f"{label} must be true or false.")
    precedence = data.get("precedence", 100)
    if isinstance(precedence, bool) or not isinstance(precedence, int) or not 0 <= precedence <= 100000:
        problems.append("precedence: a whole number from 0 to 100000 (lower wins).")
    channels = data.get("channels")
    if not isinstance(channels, list) or not channels or len(set(channels)) != len(channels) or any(c not in k.Channel.values for c in channels):
        problems.append(f"channels: a non-empty list of distinct values from {', '.join(k.Channel.values)}.")
    priority = str(data.get("priority", k.Priority.NORMAL))
    if priority not in k.Priority.values:
        problems.append(f"priority must be one of: {', '.join(k.Priority.values)}.")
    policy = str(data.get("preference_policy", k.PreferencePolicy.OPTIONAL))
    if policy not in k.PreferencePolicy.values:
        problems.append(f"preference_policy must be one of: {', '.join(k.PreferencePolicy.values)}.")
    strategies, found = recipients.clean_strategies(data.get("recipient_strategies"), organization)
    problems.extend(found)
    clean_filters: list[dict[str, Any]] = []
    if event is not None:
        clean_filters, found = filters.clean_filters(event, data.get("filters", []))
        problems.extend(found)
    template_code = str(data.get("template_code", "") or "").strip()
    if template_code:
        usable = NotificationTemplate.objects.filter(code=template_code, status=k.TemplateStatus.ACTIVE).filter(organization__in=[organization, None]).exists()
        if not usable:
            problems.append(f"template_code: there is no active template “{template_code}”.")
        elif event is not None and not NotificationTemplate.objects.filter(code=template_code, status=k.TemplateStatus.ACTIVE, event_type=event.code).filter(organization__in=[organization, None]).exists():
            problems.append(f"template_code: the template “{template_code}” is not written for the event “{event.code}”.")
    out.update(name=name, description=description, event_type=event.code if event else "", enabled=bool(enabled), precedence=precedence if isinstance(precedence, int) else 100,
               recipient_strategies=strategies, channels=list(channels) if isinstance(channels, list) else [], template_code=template_code, priority=priority, preference_policy=policy,
               filters=clean_filters, exclude_actor=bool(exclude_actor))
    return out, problems


@transaction.atomic
def save_rule(organization: Any, *, actor: Any, data: dict[str, Any], code: str | None = None, rule: NotificationRule | None = None) -> NotificationRule:
    """Create (``code`` required) or update (``rule``) a rule.  Audited; the revision grows with every change."""
    if not policies.can_configure(actor, organization):
        raise PermissionDenied
    unknown = set(data) - set(RULE_FIELDS) - {"code"}
    if unknown:
        raise NotificationError("Unknown fields: " + ", ".join(sorted(unknown)), "unknown_field")
    base = {f: getattr(rule, f) for f in RULE_FIELDS} if rule is not None else {}
    cleaned, problems = clean_rule(organization, {**base, **data})
    if problems:
        raise NotificationError(" ".join(problems), "invalid_rule", extra={"problems": problems})
    if cleaned["preference_policy"] == k.PreferencePolicy.MANDATORY and (rule is None or rule.preference_policy != k.PreferencePolicy.MANDATORY):
        if not policies.can_set_mandatory(actor, organization):
            raise PermissionDenied
    if rule is None:
        slug = str(code or "").strip()
        if not slug or len(slug) > 60 or not all(c.isalnum() or c in "-_" for c in slug):
            raise NotificationError("A rule code is 1 to 60 letters, digits, dashes or underscores.", "invalid_code")
        rule = NotificationRule(organization=organization, code=slug, created_by=actor)
        created = True
    else:
        if rule.organization_id != organization.pk:
            raise PermissionDenied
        rule = NotificationRule.objects.select_for_update().get(pk=rule.pk)
        rule.revision += 1
        created = False
    for field, value in cleaned.items():
        setattr(rule, field, value)
    rule.updated_by = actor
    stamp_history(rule, actor, VERBS["rule_saved"])
    try:
        with transaction.atomic():
            rule.save()
    except IntegrityError as exc:
        raise NotificationConflict("A rule with this code already exists.", "duplicate_code") from exc
    audit(VERBS["rule_saved"], organization, actor, rule, f"Notification rule {rule.code} {'created' if created else 'updated'}", rule=rule.code, event_type=rule.event_type, enabled=rule.enabled,
          channels=rule.channels, policy=rule.preference_policy, revision=rule.revision)
    return rule


def set_rule_enabled(rule: NotificationRule, *, actor: Any, enabled: bool) -> NotificationRule:
    return save_rule(rule.organization, actor=actor, data={"enabled": enabled}, rule=rule)


# ---------------------------------------------------------------------------------------------------------------------------------
# Templates
# ---------------------------------------------------------------------------------------------------------------------------------
def template_problems(event_type: str, fields: dict[str, str]) -> list[str]:
    event = registry.get_event(event_type)
    if event is None:
        return [f"event_type must be one of: {', '.join(registry.EVENTS)}."]
    return templating.validate({name: str(fields.get(name, "") or "") for name in templating.FIELDS}, event.variables)


@transaction.atomic
def save_template(organization: Any, *, actor: Any, code: str, name: str, event_type: str, fields: dict[str, str], locale: str = "en") -> NotificationTemplate:
    """Create or update the **draft** of template ``code``.  If only an active version exists, the next draft version is created from the given fields."""
    if not policies.can_configure(actor, organization):
        raise PermissionDenied
    slug = str(code or "").strip()
    if not slug or len(slug) > 60 or not all(c.isalnum() or c in "-_" for c in slug):
        raise NotificationError("A template code is 1 to 60 letters, digits, dashes or underscores.", "invalid_code")
    if registry.get_event(event_type) is None:
        raise NotificationError(f"event_type must be one of: {', '.join(registry.EVENTS)}.", "invalid_event")
    if not str(name or "").strip() or len(name) > 120:
        raise NotificationError("A template needs a name of 1 to 120 characters.", "invalid_name")
    unknown = set(fields) - set(templating.FIELDS)
    if unknown:
        raise NotificationError("Unknown template fields: " + ", ".join(sorted(unknown)), "unknown_field")
    qs = NotificationTemplate.objects.select_for_update().filter(organization=organization, code=slug)
    draft = qs.filter(status=k.TemplateStatus.DRAFT).first()
    existing = qs.order_by("-version").first()
    if existing is not None and existing.event_type != event_type:
        raise NotificationError("A template's event type cannot change; create a new template code.", "event_locked")
    values = {name_: str(fields.get(name_, "") or "") for name_ in templating.FIELDS}
    if draft is None:
        version = (qs.aggregate(top=Max("version"))["top"] or 0) + 1
        draft = NotificationTemplate(organization=organization, code=slug, version=version, status=k.TemplateStatus.DRAFT, created_by=actor)
    draft.name, draft.event_type, draft.locale, draft.updated_by = name.strip(), event_type, locale[:10] or "en", actor
    for field, value in values.items():
        setattr(draft, field, value)
    stamp_history(draft, actor, VERBS["template_saved"])
    draft.save()
    audit(VERBS["template_saved"], organization, actor, draft, f"Notification template {slug} v{draft.version} saved as a draft", template=slug, version=draft.version, event_type=event_type)
    return draft


@transaction.atomic
def activate_template(template: NotificationTemplate, *, actor: Any) -> NotificationTemplate:
    """Validate and activate a draft; the previously active version (same code, same organization) is archived."""
    organization = template.organization
    if organization is None or not policies.can_configure(actor, organization):
        raise PermissionDenied
    template = NotificationTemplate.objects.select_for_update().get(pk=template.pk)
    if template.status != k.TemplateStatus.DRAFT:
        raise NotificationConflict("Only a draft can be activated.", "not_draft")
    problems = template_problems(template.event_type, {f: getattr(template, f) for f in templating.FIELDS})
    if problems:
        raise NotificationError(" ".join(problems), "invalid_template", extra={"problems": problems})
    for old in NotificationTemplate.objects.select_for_update().filter(organization=organization, code=template.code, status=k.TemplateStatus.ACTIVE):
        old.status = k.TemplateStatus.ARCHIVED
        stamp_history(old, actor, VERBS["template_archived"])
        old.save(update_fields=["status", "updated_at"])
    template.status = k.TemplateStatus.ACTIVE
    template.updated_by = actor
    stamp_history(template, actor, VERBS["template_activated"])
    template.save()
    audit(VERBS["template_activated"], organization, actor, template, f"Notification template {template.code} v{template.version} activated", template=template.code, version=template.version)
    return template


@transaction.atomic
def archive_template(template: NotificationTemplate, *, actor: Any) -> NotificationTemplate:
    organization = template.organization
    if organization is None or not policies.can_configure(actor, organization):
        raise PermissionDenied
    template = NotificationTemplate.objects.select_for_update().get(pk=template.pk)
    if template.status == k.TemplateStatus.ARCHIVED:
        return template
    template.status = k.TemplateStatus.ARCHIVED
    stamp_history(template, actor, VERBS["template_archived"])
    template.save(update_fields=["status", "updated_at"])
    audit(VERBS["template_archived"], organization, actor, template, f"Notification template {template.code} v{template.version} archived", template=template.code, version=template.version)
    return template


def active_template(organization: Any, code: str, event_type: str) -> NotificationTemplate | None:
    """The active template ``code`` for ``event_type``: the organization's own, else a system one; ``None`` = use the built-in default."""
    if not code:
        return None
    candidates = NotificationTemplate.objects.filter(code=code, status=k.TemplateStatus.ACTIVE, event_type=event_type)
    return candidates.filter(organization=organization).first() or candidates.filter(organization__isnull=True).first()


def fields_for(event: registry.Event, template: NotificationTemplate | None) -> dict[str, str]:
    """The text to render: the template's fields, completed per channel from the event's built-in default (the documented fallback)."""
    default = {"subject": event.subject, "body_text": event.body, "body_html": "", "in_app_title": event.title, "in_app_message": event.message}
    if template is None:
        return default
    out = {f: getattr(template, f) or "" for f in templating.FIELDS}
    if not (out["subject"].strip() and out["body_text"].strip()):
        out["subject"], out["body_text"], out["body_html"] = default["subject"], default["body_text"], ""
    if not (out["in_app_title"].strip() and out["in_app_message"].strip()):
        out["in_app_title"], out["in_app_message"] = default["in_app_title"], default["in_app_message"]
    return out


def preview(event_type: str, fields: dict[str, str]) -> dict[str, Any]:
    """Validate and render ``fields`` with synthetic values (never real data)."""
    event = registry.get_event(event_type)
    if event is None:
        raise NotificationError(f"event_type must be one of: {', '.join(registry.EVENTS)}.", "invalid_event")
    values = {name: str(fields.get(name, "") or "") for name in templating.FIELDS}
    problems = templating.validate(values, event.variables)
    if problems:
        return {"valid": False, "problems": problems, "rendered": None, "variables": list(event.variables)}
    return {"valid": True, "problems": [], "rendered": templating.render(values, templating.sample_context(event.variables)), "variables": list(event.variables)}
