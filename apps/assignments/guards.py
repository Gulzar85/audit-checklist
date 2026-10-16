"""PostgreSQL integrity trigger for assignments - what CHECK constraints cannot express (ADR 0051).

* the organization of the assignment, of the version's template and of the location must be the same;
* the organization never changes; version and location never change once the assignment has left ``draft``;
* a non-draft assignment is never deleted (history).

Installed idempotently by a ``post_migrate`` handler.  Limits: a database superuser can drop the trigger, and
``TRUNCATE`` ignores it (see ADR 0049 for the same stance on checklist definitions).
"""

from __future__ import annotations

from django.db import connections


def install_database_guard(using: str = "default") -> bool:
    from apps.checklists.models import ChecklistTemplate, ChecklistVersion
    from apps.locations.models import Location

    from .models import ChecklistAssignment

    connection = connections[using]
    if connection.vendor != "postgresql":
        return False
    table = ChecklistAssignment._meta.db_table
    version, template, location = ChecklistVersion._meta.db_table, ChecklistTemplate._meta.db_table, Location._meta.db_table
    with connection.cursor() as cursor:
        if not {table, version, template, location} <= set(connection.introspection.table_names(cursor)):
            return False
        cursor.execute(f"""
CREATE OR REPLACE FUNCTION assignments_guard() RETURNS trigger AS $$
DECLARE
  v_org uuid;
  l_org uuid;
BEGIN
  IF TG_OP = 'DELETE' THEN
    IF OLD.status <> 'draft' THEN
      RAISE EXCEPTION 'An issued assignment is part of the history and cannot be deleted.'
        USING ERRCODE = 'check_violation', CONSTRAINT = 'assignment_history_protected';
    END IF;
    RETURN OLD;
  END IF;
  SELECT t.organization_id INTO v_org FROM "{version}" v JOIN "{template}" t ON t.id = v.template_id WHERE v.id = NEW.version_id;
  SELECT organization_id INTO l_org FROM "{location}" WHERE id = NEW.location_id;
  IF v_org IS DISTINCT FROM NEW.organization_id OR l_org IS DISTINCT FROM NEW.organization_id THEN
    RAISE EXCEPTION 'The checklist version and the location must belong to the assignment''s organization.'
      USING ERRCODE = 'check_violation', CONSTRAINT = 'assignment_organization_consistent';
  END IF;
  IF TG_OP = 'UPDATE' THEN
    IF NEW.organization_id <> OLD.organization_id THEN
      RAISE EXCEPTION 'The organization of an assignment never changes.'
        USING ERRCODE = 'check_violation', CONSTRAINT = 'assignment_identity_immutable';
    END IF;
    IF OLD.status <> 'draft' AND (NEW.version_id <> OLD.version_id OR NEW.location_id <> OLD.location_id) THEN
      RAISE EXCEPTION 'The version and location of an issued assignment never change.'
        USING ERRCODE = 'check_violation', CONSTRAINT = 'assignment_identity_immutable';
    END IF;
  END IF;
  RETURN NEW;
END
$$ LANGUAGE plpgsql;
""")
        cursor.execute(f'DROP TRIGGER IF EXISTS assignments_guard_trg ON "{table}"')
        cursor.execute(
            f'CREATE TRIGGER assignments_guard_trg BEFORE INSERT OR UPDATE OR DELETE ON "{table}" '
            "FOR EACH ROW EXECUTE FUNCTION assignments_guard()"
        )
    return True
