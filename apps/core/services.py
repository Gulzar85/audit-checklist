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

from typing import TYPE_CHECKING, Any

from .models import ActivityLog

if TYPE_CHECKING:  # pragma: no cover - typing only
    from apps.organizations.models import Organization


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
