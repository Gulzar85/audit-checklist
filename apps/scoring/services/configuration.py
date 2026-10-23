"""Writing and validating the scoring configuration of a *draft* checklist version.

Every write locks the template through the checklists service (the same lock every draft edit takes), refuses anything but a draft,
stores the **normalized** document (so equivalent input hashes identically), and logs through the shared activity log.  A draft may be
saved while incomplete; completeness is what ``validate`` and publication check.
"""

from __future__ import annotations

import copy
from typing import Any

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction

from apps.checklists import services as checklist_services
from apps.checklists.rules import snapshot_for_version
from apps.core.services import log_activity, stamp_history

from .. import config as cfg
from .. import constants as k
from .. import policies
from ..models import ScoringConfig
from .errors import ScoringError

VERBS = {
    "saved": "scoring.config_saved", "removed": "scoring.config_removed", "validated": "scoring.config_validated",
    "cloned": "scoring.config_cloned",
}
SETTING_KEYS = ("enabled", "method", "na_policy", "missing_policy", "precision", "critical_overrides_grade", "critical_label")


def _log(verb: str, version: Any, actor: Any, description: str, **meta: Any) -> None:
    log_activity(verb=verb, organization=version.template.organization, actor=actor, target=version, description=description,
                 metadata={"template_id": str(version.template_id), "version": version.version_number, **meta})


def _clean_settings(raw: dict[str, Any], base: dict[str, Any]) -> dict[str, Any]:
    unknown = set(raw) - set(SETTING_KEYS)
    if unknown:
        raise ScoringError(f"Unknown scoring settings: {', '.join(sorted(unknown))}.", "unknown_setting")
    merged = {**base, **raw}
    for key in ("enabled", "critical_overrides_grade"):
        if not isinstance(merged[key], bool):
            raise ScoringError(f"“{key}” must be true or false.", "invalid_setting")
    for key, choices in (("method", k.Method), ("na_policy", k.NaPolicy), ("missing_policy", k.MissingPolicy)):
        if merged[key] not in choices.values:
            raise ScoringError(f"“{merged[key]}” is not a supported {key.replace('_', ' ')}.", "invalid_setting")
    precision = merged["precision"]
    if not isinstance(precision, int) or isinstance(precision, bool) or not 0 <= precision <= 4:
        raise ScoringError("The rounding precision must be a whole number from 0 to 4.", "invalid_setting")
    merged["critical_label"] = str(merged.get("critical_label") or "").strip()
    if len(merged["critical_label"]) > 100:
        raise ScoringError("The critical outcome label can have at most 100 characters.", "invalid_setting")
    return merged


def _settings_of(row: ScoringConfig | None) -> dict[str, Any]:
    return dict(row.settings) if row is not None else cfg.default_settings()


@transaction.atomic
def save_config(version: Any, *, actor: Any, settings: dict[str, Any] | None = None, document: Any = None,
                notes: str | None = None) -> ScoringConfig:
    """Create or update the draft's scoring configuration (``settings`` / ``document`` replace the given parts only).

    ``document`` may be a callable ``f(current) -> new`` to edit one part atomically (see the ``set_*`` helpers below).
    """
    if not policies.can_configure_at(actor, version):
        raise PermissionDenied
    template, fresh = _editable(version)
    row = ScoringConfig.objects.select_for_update().filter(version=fresh).first()
    merged = _clean_settings(settings or {}, _settings_of(row))
    current = copy.deepcopy(row.document) if row else cfg.empty_document()
    if callable(document):  # an edit of one part, applied to the document as locked and read here (no lost updates)
        document = document(current)
    try:
        normalized = cfg.to_document(cfg.parse(merged, document if document is not None else current))
    except cfg.ConfigError as exc:
        raise ScoringError(str(exc), "malformed") from exc
    created = row is None
    if created:
        row = ScoringConfig(version=fresh, organization_id=template.organization_id, created_by=actor)
    for key in SETTING_KEYS:
        setattr(row, key, merged[key])
    row.document = normalized
    if notes is not None:
        row.notes = notes.strip()[:500]
    row.updated_by = actor
    stamp_history(row, actor, VERBS["saved"])
    row.save()
    checklist_services._touch(template, actor)
    _log(VERBS["saved"], fresh, actor, f"Scoring {'created' if created else 'updated'} on {template.code} v{fresh.version_number}",
         scored_items=len(normalized["items"]), bands=len(normalized["bands"]), critical=len(normalized["critical"]), enabled=row.enabled)
    return row


def set_item_rule(version: Any, *, actor: Any, code: str, rule: dict[str, Any] | None) -> ScoringConfig:
    """Set (or, with ``None``, remove) the scoring rule of one question."""
    def edit(doc: dict[str, Any]) -> dict[str, Any]:
        items = dict(doc.get("items") or {})
        if rule is None:
            items.pop(code, None)
        else:
            items[code] = rule
        return {**doc, "items": items}

    return save_config(version, actor=actor, document=edit)


def set_part(version: Any, *, actor: Any, part: str, value: Any) -> ScoringConfig:
    """Replace one whole part of the document (``sections`` weights, ``bands`` or ``critical`` rules)."""
    if part not in ("sections", "bands", "critical"):
        raise ScoringError("Unknown part of the scoring configuration.", "unknown_part")
    return save_config(version, actor=actor, document=lambda doc: {**doc, part: value})


@transaction.atomic
def remove_config(version: Any, *, actor: Any) -> None:
    if not policies.can_configure_at(actor, version):
        raise PermissionDenied
    template, fresh = _editable(version)
    row = ScoringConfig.objects.select_for_update().filter(version=fresh).first()
    if row is None:
        return
    row.delete()
    checklist_services._touch(template, actor)
    _log(VERBS["removed"], fresh, actor, f"Scoring removed from {template.code} v{fresh.version_number}")


def _editable(version: Any) -> tuple[Any, Any]:
    try:
        return checklist_services._editable(version)
    except ValidationError as exc:
        raise ScoringError(" ".join(exc.messages), getattr(exc, "code", None) or "version_immutable") from exc


def validate_config(version: Any, *, settings: dict[str, Any] | None = None, document: dict[str, Any] | None = None) -> list[cfg.Issue]:
    """Issues of the saved configuration (or of a candidate ``settings`` / ``document``) against the version's real questions."""
    row = ScoringConfig.objects.filter(version=version).first()
    if row is None and settings is None and document is None:
        return []
    merged = _clean_settings(settings or {}, _settings_of(row))
    doc = document if document is not None else (row.document if row else cfg.empty_document())
    return cfg.validate(merged, doc, snapshot_for_version(version, include_rules=False))


# ---------------------------------------------------------------------------
# Hooks into the version lifecycle (registered by ``integration``)
# ---------------------------------------------------------------------------
def manifest_extension(version: Any) -> tuple[str, Any] | None:
    row = ScoringConfig.objects.filter(version=version).first()
    return None if row is None else ("scoring", cfg.canonical(row.settings, row.document))


def publication_findings(version: Any) -> list[tuple[str, str, str, str]]:
    row = ScoringConfig.objects.filter(version=version).first()
    if row is None:
        return []
    out = []
    for issue in cfg.validate(row.settings, row.document, snapshot_for_version(version, include_rules=False)):
        out.append((issue.level, f"scoring_{issue.code}", f"Scoring: {issue.message}", issue.subject))
    return out


def clone_config(source: Any, target: Any, actor: Any) -> dict[str, int]:
    row = ScoringConfig.objects.filter(version=source).first()
    if row is None:
        return {"scoring": 0}
    copy = ScoringConfig(version=target, organization_id=row.organization_id, enabled=row.enabled, method=row.method, na_policy=row.na_policy,
                         missing_policy=row.missing_policy, precision=row.precision, critical_overrides_grade=row.critical_overrides_grade,
                         critical_label=row.critical_label, document=row.document, notes=row.notes,
                         created_by=actor if getattr(actor, "is_authenticated", False) else None)
    stamp_history(copy, actor, VERBS["cloned"])
    copy.save()
    return {"scoring": 1}


__all__ = ["SETTING_KEYS", "VERBS", "clone_config", "manifest_extension", "publication_findings", "remove_config", "save_config", "set_item_rule", "set_part", "validate_config"]
