"""Service-layer conventions and shared business services for the core app.

**Architecture rules**

* ``selectors.py``  — read-only query helpers (no side effects).
* ``services.py``   — business operations that may write; the only place
  business rules should live.
* ``policies.py``   — authorization / business-permission checks.
* ``tasks/``        — background or scheduled entrypoints that *call* services.

Views, templates, signals and model ``save()`` methods must not contain business
logic.
"""

from __future__ import annotations

import datetime
import decimal
import uuid
from typing import TYPE_CHECKING, Any

from django.db import models

from .models import ActivityLog

if TYPE_CHECKING:  # pragma: no cover - typing only
    from apps.organizations.models import Organization


def _jsonable(value: Any) -> Any:
    """Return a JSON-safe representation of a field value for the activity log."""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, (uuid.UUID, decimal.Decimal)):
        return str(value)
    if isinstance(value, (datetime.datetime, datetime.date)):
        return value.isoformat()
    return str(value)


def apply_changes(instance: models.Model, data: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Assign changed values from ``data`` onto ``instance``.

    Returns ``{field: {"from": old, "to": new}}`` for the fields that actually
    changed, in a JSON-safe form suitable for ``ActivityLog.metadata``.  Nothing
    is saved.  Relations are compared by primary key, and file fields by file
    name (an uploaded file is recorded as ``"(new file)"`` because its final
    storage name is not known until it is saved).
    """
    changes: dict[str, dict[str, Any]] = {}
    for name, new in data.items():
        field = instance._meta.get_field(name)

        if field.is_relation and field.many_to_one:
            old_pk = getattr(instance, field.attname)
            new_pk = getattr(new, "pk", new)
            if old_pk == new_pk:
                continue
            setattr(instance, name, new)
            changes[name] = {"from": _jsonable(old_pk), "to": _jsonable(new_pk)}
        elif isinstance(field, models.FileField):
            old_name = getattr(instance, name).name or ""
            if new is False:  # the form's "clear" box was ticked
                if old_name:
                    setattr(instance, name, None)
                    changes[name] = {"from": old_name, "to": ""}
            elif new is None or getattr(new, "_committed", False):
                continue  # nothing supplied, or an already-stored file handed back
            else:
                setattr(instance, name, new)
                changes[name] = {"from": old_name, "to": "(new file)"}
        else:
            old = getattr(instance, name)
            if old == new:
                continue
            setattr(instance, name, new)
            changes[name] = {"from": _jsonable(old), "to": _jsonable(new)}
    return changes


def stamp_history(instance: models.Model, actor: Any = None, reason: str = "") -> None:
    """Tell django-simple-history who made the change and why.

    Inside a request the history middleware already knows the user; this also
    covers services called from commands, imports and tests.
    """
    if getattr(actor, "is_authenticated", False):
        instance._history_user = actor  # type: ignore[attr-defined]
    if reason:
        instance._change_reason = reason[:100]  # type: ignore[attr-defined]


def log_activity(
    *,
    verb: str,
    organization: "Organization | None" = None,
    actor: Any = None,
    description: str = "",
    target: Any = None,
    metadata: dict[str, Any] | None = None,
) -> ActivityLog:
    """Record a business activity event.

    This is the single sanctioned way to write to :class:`ActivityLog`.  It
    records *business* events (``organization.created``, ``location.moved`` …)
    and is intentionally separate from per-model field history
    (django-simple-history).

    ``actor`` may be any user-like object; anonymous/``None`` actors are stored
    as ``NULL``.  ``target`` is any model instance and is stored as a
    ``(label, id)`` pair so the log stays generic across domains.
    """
    actor_obj = actor if getattr(actor, "is_authenticated", False) else None
    target_type = ""
    target_id = ""
    if target is not None:
        target_type = target._meta.label_lower
        target_id = str(target.pk)

    return ActivityLog.objects.create(
        organization=organization,
        actor=actor_obj,
        verb=verb,
        description=description[:512],
        target_type=target_type,
        target_id=target_id,
        metadata=metadata or {},
    )
