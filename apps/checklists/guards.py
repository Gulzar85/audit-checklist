"""Immutability of published checklist definitions - the layers below the services.

A published (or superseded) version is a stable, reproducible definition.  The services refuse to change one (friendly
errors, locking, audit), and the models refuse too (``DefinitionGuardMixin``: ``save()`` / ``delete()``).  Neither stops a
``QuerySet.update()``, a ``bulk_create`` or hand-written SQL, so PostgreSQL is the last line:

* ``checklists_guard_definition`` - a ``BEFORE INSERT/UPDATE/DELETE`` trigger on sections, items, options and rules that
  raises ``check_violation`` (``checklist_definition_immutable``) unless the owning version is a **draft**;
* ``checklists_guard_version`` - a trigger on versions: a draft may be edited and published (or deleted, i.e. discarded);
  a published version may only become *superseded*; a superseded one never changes; neither can be deleted, and none of
  their definition columns (template, number, summary, publication metadata, base, integrity hash and report) change.

The triggers are (re)installed idempotently by a ``post_migrate`` handler (``ChecklistsConfig.ready``), so every environment
- including fresh checkouts and test databases, whatever migration files exist locally - has them after ``migrate``.

Boundaries, stated honestly
---------------------------
* A database **superuser or the table owner** can disable or drop the triggers, ``TRUNCATE`` ignores row triggers, and a
  restore from backup replaces data wholesale.  These are administrative controls (database roles, backups); the
  application cannot defend against its own database owner.
* **Maintenance** (for example sealing the integrity hash of versions published before hashes existed) is possible only
  through :func:`allow_definition_changes`, an explicit, transaction-scoped, logged override that application code never
  enables on its own.
* Rows are protected; the *historical* tables of ``simple_history`` are append-only by design and unaffected.
"""

from __future__ import annotations

import contextvars
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from django.core.exceptions import ValidationError
from django.db import connections, transaction
from django.utils.translation import gettext_lazy as _

from . import constants as c

_MAINTENANCE: contextvars.ContextVar[bool] = contextvars.ContextVar("checklists_definition_maintenance", default=False)
SETTING = "checklists.allow_definition_change"


# ---------------------------------------------------------------------------
# Application-level guard (models)
# ---------------------------------------------------------------------------
def version_status_of(obj: Any) -> str | None:
    """Status of the version an object belongs to (``None`` when it cannot be resolved, e.g. a brand-new orphan)."""
    from .models import ChecklistItem, ChecklistSection, ChecklistVersion

    if isinstance(obj, ChecklistVersion):
        return obj.status
    if getattr(obj, "version_id", None):  # section, rule
        return ChecklistVersion.objects.filter(pk=obj.version_id).values_list("status", flat=True).first()
    if getattr(obj, "section_id", None):  # item
        return ChecklistSection.objects.filter(pk=obj.section_id).values_list("version__status", flat=True).first()
    if getattr(obj, "item_id", None):  # option
        return ChecklistItem.objects.filter(pk=obj.item_id).values_list("section__version__status", flat=True).first()
    return None


def definition_changes_allowed() -> bool:
    return _MAINTENANCE.get()


def assert_editable(obj: Any) -> None:
    """Raise unless ``obj`` belongs to a draft version (or an explicit maintenance override is active)."""
    if definition_changes_allowed():
        return
    status = version_status_of(obj)
    if status is not None and status != c.VersionStatus.DRAFT:
        raise ValidationError(
            _("This checklist version is already published and cannot be edited. Create a new draft version to make changes."),
            code="version_immutable",
        )


class DefinitionGuardMixin:
    """Model mixin: a section, item, option or rule of a non-draft version cannot be saved or deleted."""

    def save(self, *args: Any, **kwargs: Any) -> None:
        assert_editable(self)
        super().save(*args, **kwargs)  # type: ignore[misc]

    def delete(self, *args: Any, **kwargs: Any) -> Any:
        assert_editable(self)
        return super().delete(*args, **kwargs)  # type: ignore[misc]


@contextmanager
def allow_definition_changes(using: str = "default") -> Iterator[None]:
    """Explicit maintenance override (application *and* database guards) for the enclosed transaction.

    Only for audited maintenance such as sealing legacy versions; callers must log what they do.
    """
    token = _MAINTENANCE.set(True)
    try:
        with transaction.atomic(using=using):
            with connections[using].cursor() as cursor:
                cursor.execute("SELECT set_config(%s, 'on', true)", [SETTING])
            try:
                yield
            finally:
                # ``set_config(..., true)`` lasts until the end of the *outermost* transaction; switch it off explicitly so a
                # caller that was already inside a transaction does not leave the override on after this block.
                with connections[using].cursor() as cursor:
                    cursor.execute("SELECT set_config(%s, 'off', true)", [SETTING])
    finally:
        _MAINTENANCE.reset(token)


# ---------------------------------------------------------------------------
# PostgreSQL triggers
# ---------------------------------------------------------------------------
def _sql(tables: dict[str, str]) -> list[str]:
    section, item, option, rule, version = (tables[k] for k in ("section", "item", "option", "rule", "version"))
    return [
        f"""
CREATE OR REPLACE FUNCTION checklists_row_version_status(tbl text, r jsonb) RETURNS text AS $$
  SELECT CASE tbl
    WHEN '{section}' THEN (SELECT v.status FROM "{version}" v WHERE v.id = (r->>'version_id')::uuid)
    WHEN '{rule}' THEN (SELECT v.status FROM "{version}" v WHERE v.id = (r->>'version_id')::uuid)
    WHEN '{item}' THEN (SELECT v.status FROM "{section}" s JOIN "{version}" v ON v.id = s.version_id WHERE s.id = (r->>'section_id')::uuid)
    WHEN '{option}' THEN (SELECT v.status FROM "{item}" i JOIN "{section}" s ON s.id = i.section_id
                          JOIN "{version}" v ON v.id = s.version_id WHERE i.id = (r->>'item_id')::uuid)
  END
$$ LANGUAGE sql STABLE;
""",
        f"""
CREATE OR REPLACE FUNCTION checklists_guard_definition() RETURNS trigger AS $$
DECLARE
  s_old text;
  s_new text;
BEGIN
  IF current_setting('{SETTING}', true) = 'on' THEN
    RETURN CASE WHEN TG_OP = 'DELETE' THEN OLD ELSE NEW END;
  END IF;
  IF TG_OP <> 'INSERT' THEN s_old := checklists_row_version_status(TG_TABLE_NAME, to_jsonb(OLD)); END IF;
  IF TG_OP <> 'DELETE' THEN s_new := checklists_row_version_status(TG_TABLE_NAME, to_jsonb(NEW)); END IF;
  IF (s_old IS NOT NULL AND s_old <> 'draft') OR (s_new IS NOT NULL AND s_new <> 'draft') THEN
    RAISE EXCEPTION 'A published checklist version cannot be changed; create a new draft version instead.'
      USING ERRCODE = 'check_violation', CONSTRAINT = 'checklist_definition_immutable';
  END IF;
  RETURN CASE WHEN TG_OP = 'DELETE' THEN OLD ELSE NEW END;
END
$$ LANGUAGE plpgsql;
""",
        f"""
CREATE OR REPLACE FUNCTION checklists_guard_version() RETURNS trigger AS $$
BEGIN
  IF current_setting('{SETTING}', true) = 'on' THEN
    RETURN CASE WHEN TG_OP = 'DELETE' THEN OLD ELSE NEW END;
  END IF;
  IF TG_OP = 'INSERT' THEN
    RETURN NEW;
  END IF;
  IF TG_OP = 'DELETE' THEN
    IF OLD.status <> 'draft' THEN
      RAISE EXCEPTION 'A published checklist version cannot be deleted.'
        USING ERRCODE = 'check_violation', CONSTRAINT = 'checklist_version_immutable';
    END IF;
    RETURN OLD;
  END IF;
  -- UPDATE
  IF OLD.status = 'draft' THEN
    IF NEW.status NOT IN ('draft', 'published') THEN
      RAISE EXCEPTION 'A draft version can only be published.'
        USING ERRCODE = 'check_violation', CONSTRAINT = 'checklist_version_transition';
    END IF;
    RETURN NEW;
  END IF;
  -- published / superseded: the definition and its publication record never change
  IF NEW.template_id IS DISTINCT FROM OLD.template_id OR NEW.version_number IS DISTINCT FROM OLD.version_number
     OR NEW.change_summary IS DISTINCT FROM OLD.change_summary OR NEW.published_at IS DISTINCT FROM OLD.published_at
     OR NEW.based_on_id IS DISTINCT FROM OLD.based_on_id OR NEW.integrity_hash IS DISTINCT FROM OLD.integrity_hash
     OR NEW.publication_report IS DISTINCT FROM OLD.publication_report THEN
    RAISE EXCEPTION 'A published checklist version cannot be changed; create a new draft version instead.'
      USING ERRCODE = 'check_violation', CONSTRAINT = 'checklist_version_immutable';
  END IF;
  IF OLD.status = 'published' AND NEW.status = 'superseded' THEN
    RETURN NEW;
  END IF;
  IF NEW.status = OLD.status AND NEW.superseded_at IS NOT DISTINCT FROM OLD.superseded_at THEN
    RETURN NEW;
  END IF;
  RAISE EXCEPTION 'This lifecycle transition is not allowed (published -> superseded is the only one).'
    USING ERRCODE = 'check_violation', CONSTRAINT = 'checklist_version_transition';
END
$$ LANGUAGE plpgsql;
""",
    ]


def install_database_guards(using: str = "default") -> bool:
    """Create or refresh the triggers.  Returns False (and does nothing) when the schema is not ready or not PostgreSQL."""
    from .models import ChecklistItem, ChecklistOption, ChecklistRule, ChecklistSection, ChecklistVersion

    connection = connections[using]
    if connection.vendor != "postgresql":
        return False
    tables = {
        "section": ChecklistSection._meta.db_table, "item": ChecklistItem._meta.db_table,
        "option": ChecklistOption._meta.db_table, "rule": ChecklistRule._meta.db_table, "version": ChecklistVersion._meta.db_table,
    }
    with connection.cursor() as cursor:
        existing = set(connection.introspection.table_names(cursor))
        if not set(tables.values()) <= existing:
            return False
        columns = {col.name for col in connection.introspection.get_table_description(cursor, tables["version"])}
        if not {"integrity_hash", "publication_report", "superseded_at", "based_on_id"} <= columns:
            return False  # an older schema (migrating backwards / not yet migrated): nothing to guard yet
        for statement in _sql(tables):
            cursor.execute(statement)
        for kind in ("section", "item", "option", "rule"):
            table = tables[kind]
            cursor.execute(f'DROP TRIGGER IF EXISTS checklists_definition_guard ON "{table}"')
            cursor.execute(
                f'CREATE TRIGGER checklists_definition_guard BEFORE INSERT OR UPDATE OR DELETE ON "{table}" '
                f"FOR EACH ROW EXECUTE FUNCTION checklists_guard_definition()"
            )
        cursor.execute(f'DROP TRIGGER IF EXISTS checklists_version_guard ON "{tables["version"]}"')
        cursor.execute(
            f'CREATE TRIGGER checklists_version_guard BEFORE INSERT OR UPDATE OR DELETE ON "{tables["version"]}" '
            f"FOR EACH ROW EXECUTE FUNCTION checklists_guard_version()"
        )
    return True
