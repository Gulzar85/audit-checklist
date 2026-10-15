"""Business operations for the theme engine.

Every write goes through here - web UI, API, admin and imports - so validation, locking,
history and activity logging happen once (ADR 0023).  Authorization is *not* decided here;
entry points ask ``policies.py`` first.

Rules owned by this layer:

* Only **drafts** change.  Published/active themes are immutable, so a rollback restores
  exactly what ran before; a change is a *new version* (same ``code``, ``version + 1``).
* **Publishing** is blocked when mandatory WCAG contrast fails (warnings never block).
* **Activating** demotes the previous active theme to *published* (never deletes it), closes
  its ledger period and opens a new one.  Exactly one active theme per organization / system,
  enforced by the database and by a scope lock (organization row, or an advisory lock for the
  system scope).
* **Rollback** re-activates the previously active theme that is still published.
* Nothing in an exported/imported document can carry anything but validated tokens.
"""

from __future__ import annotations

import copy
from typing import Any

from django.core.exceptions import ValidationError
from django.db import IntegrityError, connection, transaction
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.core.services import log_activity, stamp_history
from apps.locations.services import lock_organization
from apps.organizations.models import Organization

from . import contrast
from . import tokens as tk
from .constants import ACTIVITY_VERBS, EXPORT_FORMAT, Appearance, ThemeScope, ThemeStatus
from .models import Theme, ThemeActivation, UserAppearance, normalize_theme_code
from .presets import PRESETS
from .resolution import effective_tokens_for
from .selectors import get_active_theme, previous_activation, user_organization

_SYSTEM_LOCK_KEY = 7_311_001  # arbitrary, app-unique advisory-lock id for the system scope
_MAX_IMPORT_BYTES = 64 * 1024


# --- helpers ------------------------------------------------------------------------------
def _lock_scope(organization: Organization | None) -> Organization | None:
    """Serialise lifecycle changes of one scope (organization row lock / system advisory lock)."""
    if organization is not None:
        return lock_organization(organization.pk)
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_advisory_xact_lock(%s)", [_SYSTEM_LOCK_KEY])
    return None


def _require_active_organization(organization: Organization | None) -> None:
    if organization is not None and not organization.is_active:
        raise ValidationError(
            {"organization": _("Organization “%(name)s” is %(status)s.")
             % {"name": organization.name, "status": organization.get_status_display().lower()}},
            code="organization_inactive",
        )


def _fresh(theme: Theme) -> Theme:
    # ``of=("self",)``: PostgreSQL cannot lock the nullable side of the outer join.
    return Theme.objects.select_for_update(of=("self",)).select_related("organization").get(pk=theme.pk)


def _as_validation_error(exc: IntegrityError) -> ValidationError:
    name = getattr(getattr(exc.__cause__, "diag", None), "constraint_name", "") or ""
    if name == "theme_unique_code_version":
        return ValidationError({"code": _("A theme with this code (and version) already exists here.")})
    return ValidationError(_("The change conflicts with the current themes. Reload and try again."), code="conflict")


def _record(verb_key: str, theme: Theme, actor: Any, description: str, metadata: dict | None = None) -> None:
    log_activity(
        verb=ACTIVITY_VERBS[verb_key],
        organization=theme.organization,
        actor=actor,
        description=description,
        target=theme,
        metadata={"code": theme.code, "version": theme.version, **(metadata or {})},
    )


def _require_status(theme: Theme, *allowed: str, action: str) -> None:
    if theme.status not in allowed:
        raise ValidationError(
            _("A %(status)s theme cannot be %(action)s.") % {"status": theme.get_status_display().lower(), "action": action},
            code="invalid_transition",
        )


def _ensure_accessible(theme: Theme) -> None:
    """Block publishing/activation when a *mandatory* contrast requirement fails."""
    failures = contrast.mandatory_failures(effective_tokens_for(theme))
    if failures:
        raise ValidationError(
            {
                "tokens": [
                    f"{check.mode.capitalize()} mode - {check.label}: {check.ratio_display} (needs {check.required:g}:1)"
                    for check in failures
                ]
            },
            code="contrast",
        )


def _unique_code(organization: Organization | None, base: str) -> str:
    base = normalize_theme_code(base) or "THEME"
    taken = set(Theme.objects.filter(organization=organization, code__startswith=base[:40]).values_list("code", flat=True))
    if base not in taken:
        return base
    for suffix in range(2, 1000):
        candidate = f"{base[:44]}_{suffix}"
        if candidate not in taken:
            return candidate
    raise ValidationError({"code": _("Could not derive a unique code; choose one.")})  # pragma: no cover


def _preset_overrides(preset_key: str) -> tuple[dict, str]:
    preset = PRESETS.get(preset_key)
    if preset is None:
        raise ValidationError({"preset": _("Unknown preset.")})
    return copy.deepcopy(preset.tokens), preset.default_appearance


def _overlay(base: dict, extra: dict | None) -> dict:
    result = copy.deepcopy(base)
    for section, values in (extra or {}).items():
        if isinstance(values, dict):
            result.setdefault(section, {}).update(values)
        else:
            result[section] = values  # validation rejects it
    return result


# --- create / update --------------------------------------------------------------------
@transaction.atomic
def create_theme(
    *,
    actor: Any = None,
    organization: Organization | None = None,
    name: str,
    code: str = "",
    description: str = "",
    preset: str = "",
    tokens: dict | None = None,
    default_appearance: str | None = None,
) -> Theme:
    """Create a new **draft** theme (optionally from a preset), version 1."""
    organization = _lock_scope(organization)
    _require_active_organization(organization)
    preset_tokens: dict = {}
    appearance = default_appearance
    if preset:
        preset_tokens, preset_appearance = _preset_overrides(preset)
        appearance = appearance or preset_appearance
    theme = Theme(
        organization=organization,
        name=name,
        code=code or _unique_code(organization, code or name),
        description=description,
        default_appearance=appearance or Appearance.SYSTEM,
        source_preset=preset,
        tokens=_overlay(preset_tokens, tokens),
        created_by=actor if getattr(actor, "is_authenticated", False) else None,
        updated_by=actor if getattr(actor, "is_authenticated", False) else None,
    )
    theme.full_clean()
    stamp_history(theme, actor, ACTIVITY_VERBS["created"])
    try:
        with transaction.atomic():
            theme.save()
    except IntegrityError as exc:
        raise _as_validation_error(exc) from exc
    _record("created", theme, actor, f"Created theme {theme.name}", {"preset": preset})
    return theme


@transaction.atomic
def update_theme(theme: Theme, *, actor: Any = None, **data: Any) -> Theme:
    """Edit a draft.  ``tokens``, when given, *replaces* the theme's override document."""
    current = _fresh(theme)
    if not current.is_editable:
        raise ValidationError(
            _("Published themes are immutable. Create a new version to change it."), code="immutable"
        )
    if data.get("code") and normalize_theme_code(data["code"]) != current.code:
        raise ValidationError({"code": _("A theme's code cannot be changed once created.")})
    data.pop("code", None)
    unknown = set(data) - {"name", "description", "default_appearance", "tokens"}
    if unknown:
        raise ValueError(f"Unsupported theme field(s): {', '.join(sorted(unknown))}")

    previous_tokens = copy.deepcopy(current.tokens)
    changes: dict[str, Any] = {}
    for field in ("name", "description", "default_appearance"):
        if field in data and data[field] != getattr(current, field):
            changes[field] = {"from": getattr(current, field), "to": data[field]}
            setattr(current, field, data[field])
    if "tokens" in data:
        current.tokens = data["tokens"]
    current.full_clean()  # normalises + validates tokens
    token_changes = tk.diff_tokens(previous_tokens, current.tokens)
    if token_changes:
        changes["tokens"] = token_changes
    if not changes:
        return current
    current.updated_by = actor if getattr(actor, "is_authenticated", False) else current.updated_by
    stamp_history(current, actor, ACTIVITY_VERBS["updated"])
    current.save()
    _record("updated", current, actor, f"Updated theme {current.name}", {"changes": changes})
    return current


# --- lifecycle ----------------------------------------------------------------------------
@transaction.atomic
def publish_theme(theme: Theme, *, actor: Any = None) -> Theme:
    """draft -> published (validates tokens and mandatory WCAG contrast)."""
    current = _fresh(theme)
    _lock_scope(current.organization)
    _require_active_organization(current.organization)
    _require_status(current, ThemeStatus.DRAFT, action="published")
    current.full_clean()
    _ensure_accessible(current)
    current.status = ThemeStatus.PUBLISHED
    current.published_at = timezone.now()
    current.updated_by = actor if getattr(actor, "is_authenticated", False) else current.updated_by
    stamp_history(current, actor, ACTIVITY_VERBS["published"])
    current.save()
    _record("published", current, actor, f"Published theme {current.name} v{current.version}")
    return current


def _activate(theme: Theme, actor: Any, via: str) -> Theme:
    """Make ``theme`` (published) the one active theme of its scope.  Caller holds the lock."""
    previous = (
        Theme.objects.select_for_update()
        .filter(status=ThemeStatus.ACTIVE, organization=theme.organization, scope=theme.scope)
        .exclude(pk=theme.pk)
        .first()
    )
    now = timezone.now()
    if previous is not None:
        previous.status = ThemeStatus.PUBLISHED  # never deleted: it stays a rollback target
        stamp_history(previous, actor, "theme.deactivated")
        previous.save(update_fields=["status", "updated_at"])
    ThemeActivation.objects.filter(organization=theme.organization, deactivated_at__isnull=True).update(
        deactivated_at=now
    )
    theme.status = ThemeStatus.ACTIVE
    theme.activated_at = now
    stamp_history(theme, actor, ACTIVITY_VERBS["rolled_back" if via == ThemeActivation.Via.ROLLBACK else "activated"])
    theme.save(update_fields=["status", "activated_at", "updated_at"])
    ThemeActivation.objects.create(
        organization=theme.organization,
        theme=theme,
        via=via,
        activated_by=actor if getattr(actor, "is_authenticated", False) else None,
    )
    return theme


@transaction.atomic
def activate_theme(theme: Theme, *, actor: Any = None) -> Theme:
    """published -> active for its organization (or system-wide); the previous one steps down."""
    scope_org = _lock_scope(theme.organization)
    current = _fresh(theme)
    _require_active_organization(scope_org)
    if current.status == ThemeStatus.ACTIVE:
        return current
    _require_status(current, ThemeStatus.PUBLISHED, action="activated")
    _ensure_accessible(current)
    previous_code = (get_active_theme(current.organization) or current).code
    _activate(current, actor, ThemeActivation.Via.ACTIVATE)
    _record("activated", current, actor, f"Activated theme {current.name} v{current.version}",
            {"replaced": previous_code if previous_code != current.code else ""})
    return current


@transaction.atomic
def rollback_theme(theme: Theme, *, actor: Any = None) -> Theme:
    """Restore the previously active theme of ``theme``'s scope; ``theme`` must be the active one."""
    scope_org = _lock_scope(theme.organization)
    current = _fresh(theme)
    _require_active_organization(scope_org)
    _require_status(current, ThemeStatus.ACTIVE, action="rolled back")
    target_row = previous_activation(current.organization, exclude_theme=current)
    if target_row is None:
        raise ValidationError(_("There is no previous theme to roll back to."), code="nothing_to_roll_back")
    target = _fresh(target_row.theme)
    if target.status != ThemeStatus.PUBLISHED:
        raise ValidationError(_("There is no previous theme to roll back to."), code="nothing_to_roll_back")
    _activate(target, actor, ThemeActivation.Via.ROLLBACK)
    _record("rolled_back", target, actor, f"Rolled back from {current.name} to {target.name} v{target.version}",
            {"from_code": current.code, "from_version": current.version})
    return target


@transaction.atomic
def archive_theme(theme: Theme, *, actor: Any = None) -> Theme:
    """draft|published -> archived.  The active theme must be replaced first."""
    current = _fresh(theme)
    _require_status(current, ThemeStatus.DRAFT, ThemeStatus.PUBLISHED, action="archived")
    current.status = ThemeStatus.ARCHIVED
    stamp_history(current, actor, ACTIVITY_VERBS["archived"])
    current.save(update_fields=["status", "updated_at"])
    _record("archived", current, actor, f"Archived theme {current.name} v{current.version}")
    return current


@transaction.atomic
def clone_theme(
    theme: Theme,
    *,
    actor: Any = None,
    name: str = "",
    organization: Organization | None | Any = ...,
) -> Theme:
    """Copy ``theme`` into a new draft with a new code (the original is never touched)."""
    target_org = theme.organization if organization is ... else organization
    if target_org is not None:
        target_org = lock_organization(target_org.pk)
    _require_active_organization(target_org)
    new_name = (name or f"{theme.name} (copy)").strip()
    clone = Theme(
        organization=target_org,
        name=new_name,
        code=_unique_code(target_org, new_name),
        description=theme.description,
        default_appearance=theme.default_appearance,
        source_preset=theme.source_preset,
        tokens=copy.deepcopy(theme.tokens),
        created_by=actor if getattr(actor, "is_authenticated", False) else None,
        updated_by=actor if getattr(actor, "is_authenticated", False) else None,
    )
    clone.full_clean()
    stamp_history(clone, actor, ACTIVITY_VERBS["cloned"])
    try:
        with transaction.atomic():
            clone.save()
    except IntegrityError as exc:
        raise _as_validation_error(exc) from exc
    _record("cloned", clone, actor, f"Cloned {theme.name} v{theme.version} as {clone.name}",
            {"source_id": str(theme.pk), "source_code": theme.code})
    return clone


@transaction.atomic
def create_new_version(theme: Theme, *, actor: Any = None) -> Theme:
    """A new draft *version* of ``theme`` (same code, version + 1) - how a published theme changes."""
    _lock_scope(theme.organization)
    current = _fresh(theme)
    siblings = Theme.objects.filter(organization=current.organization, code=current.code)
    if siblings.filter(status=ThemeStatus.DRAFT).exists():
        raise ValidationError(_("This theme already has a draft version. Finish or archive it first."), code="draft_exists")
    latest = siblings.order_by("-version").first()
    draft = Theme(
        organization=current.organization,
        name=current.name,
        code=current.code,
        version=(latest.version if latest else current.version) + 1,
        description=current.description,
        default_appearance=current.default_appearance,
        source_preset=current.source_preset,
        tokens=copy.deepcopy(current.tokens),
        created_by=actor if getattr(actor, "is_authenticated", False) else None,
        updated_by=actor if getattr(actor, "is_authenticated", False) else None,
    )
    draft.full_clean()
    stamp_history(draft, actor, ACTIVITY_VERBS["versioned"])
    try:
        with transaction.atomic():
            draft.save()
    except IntegrityError as exc:
        raise _as_validation_error(exc) from exc
    _record("versioned", draft, actor, f"Created version {draft.version} of {draft.name}",
            {"based_on": current.version})
    return draft


# --- export / import ------------------------------------------------------------------------
def export_theme(theme: Theme) -> dict[str, Any]:
    """A portable document: validated configuration only - no ids, users, organizations or secrets."""
    return {
        "format": EXPORT_FORMAT,
        "name": theme.name,
        "code": theme.code,
        "description": theme.description,
        "default_appearance": theme.default_appearance,
        "source_preset": theme.source_preset,
        "tokens": tk.validate_tokens(theme.tokens),
    }


_IMPORT_KEYS = {"format", "name", "code", "description", "default_appearance", "source_preset", "tokens"}


@transaction.atomic
def import_theme(data: Any, *, actor: Any = None, organization: Organization | None = None) -> Theme:
    """Create a draft from an exported document.  Unknown keys/tokens are rejected, not ignored."""
    if not isinstance(data, dict):
        raise ValidationError(_("The file is not a theme document."))
    if data.get("format") != EXPORT_FORMAT:
        raise ValidationError(_("Unsupported theme format."))
    extra = set(data) - _IMPORT_KEYS
    if extra:
        raise ValidationError(_("Unexpected field(s): %(fields)s.") % {"fields": ", ".join(sorted(map(str, extra))[:5])})
    name = data.get("name")
    if not isinstance(name, str) or not name.strip():
        raise ValidationError({"name": _("A theme needs a name.")})
    appearance = data.get("default_appearance", Appearance.SYSTEM)
    if appearance not in Appearance.values:
        raise ValidationError({"default_appearance": _("Unknown appearance.")})
    description = data.get("description", "")
    if not isinstance(description, str):
        raise ValidationError({"description": _("Must be text.")})
    preset_key = data.get("source_preset", "")
    theme = create_theme(
        actor=actor,
        organization=organization,
        name=name[:120],
        description=description[:500],
        tokens=data.get("tokens") or {},
        default_appearance=appearance,
    )
    if preset_key in PRESETS:
        Theme.objects.filter(pk=theme.pk).update(source_preset=preset_key)
        theme.source_preset = preset_key
    _record("imported", theme, actor, f"Imported theme {theme.name}", {"source_code": str(data.get("code", ""))[:48]})
    return theme


def parse_import_file(raw: bytes) -> Any:
    """Decode an uploaded theme document (size-capped, strict UTF-8 JSON)."""
    import json

    if len(raw) > _MAX_IMPORT_BYTES:
        raise ValidationError(_("The file is too large to be a theme document."))
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise ValidationError(_("The file is not valid JSON.")) from exc


# --- user appearance ----------------------------------------------------------------------------
@transaction.atomic
def set_user_appearance(user: Any, mode: str | None, *, actor: Any = None) -> str | None:
    """Store the user's light/dark/system choice (``None`` clears it -> inherit the theme default)."""
    if mode in (None, ""):
        mode = None
    elif mode not in Appearance.values:
        raise ValidationError({"mode": _("Choose light, dark or system.")})
    existing = UserAppearance.objects.filter(user=user).first()
    previous = existing.mode if existing else None
    if previous == mode:
        return mode
    if mode is None:
        existing.delete()
    elif existing:
        existing.mode = mode
        existing.save(update_fields=["mode", "updated_at"])
    else:
        UserAppearance.objects.create(user=user, mode=mode)
    log_activity(
        verb=ACTIVITY_VERBS["appearance"],
        organization=user_organization(user),
        actor=actor or user,
        description=f"Appearance changed to {mode or 'theme default'}",
        target=user,
        metadata={"from": previous, "to": mode},
    )
    return mode
