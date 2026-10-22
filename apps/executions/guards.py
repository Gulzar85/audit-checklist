"""PostgreSQL integrity triggers for executions and responses - what CHECK constraints cannot express (ADR 0054).

* an execution's organization and version equal its assignment's; organization, assignment and version never change;
* a submitted or cancelled execution keeps its status and is never deleted (history);
* a response's item belongs to the execution's version, and its item never changes;
* the responses of an execution that is not ``in_progress`` cannot be inserted, updated or deleted - this is what makes a
  submitted checklist immutable even for code that bypasses the services (a queryset ``update()``, the shell, the admin).

Installed idempotently by a ``post_migrate`` handler.  Limits: a database superuser can drop the triggers and ``TRUNCATE``
ignores them (the same stance as ADR 0049 / 0051).
"""

from __future__ import annotations

from django.db import connections


def install_database_guard(using: str = "default") -> bool:
    from apps.assignments.models import ChecklistAssignment
    from apps.checklists.models import ChecklistItem, ChecklistSection

    from .models import ChecklistExecution, ChecklistResponse

    connection = connections[using]
    if connection.vendor != "postgresql":
        return False
    ex, rs = ChecklistExecution._meta.db_table, ChecklistResponse._meta.db_table
    asg, item, section = ChecklistAssignment._meta.db_table, ChecklistItem._meta.db_table, ChecklistSection._meta.db_table
    with connection.cursor() as cursor:
        if not {ex, rs, asg, item, section} <= set(connection.introspection.table_names(cursor)):
            return False
        cursor.execute(f"""
CREATE OR REPLACE FUNCTION executions_guard() RETURNS trigger AS $$
DECLARE
  a_org uuid;
  a_version uuid;
BEGIN
  IF TG_OP = 'DELETE' THEN
    IF OLD.status <> 'in_progress' THEN
      RAISE EXCEPTION 'A submitted or cancelled execution is part of the history and cannot be deleted.'
        USING ERRCODE = 'check_violation', CONSTRAINT = 'execution_history_protected';
    END IF;
    RETURN OLD;
  END IF;
  SELECT organization_id, version_id INTO a_org, a_version FROM "{asg}" WHERE id = NEW.assignment_id;
  IF a_org IS DISTINCT FROM NEW.organization_id OR a_version IS DISTINCT FROM NEW.version_id THEN
    RAISE EXCEPTION 'An execution must use its assignment''s organization and exact checklist version.'
      USING ERRCODE = 'check_violation', CONSTRAINT = 'execution_assignment_consistent';
  END IF;
  IF TG_OP = 'UPDATE' THEN
    IF NEW.organization_id <> OLD.organization_id OR NEW.assignment_id <> OLD.assignment_id OR NEW.version_id <> OLD.version_id THEN
      RAISE EXCEPTION 'The organization, assignment and version of an execution never change.'
        USING ERRCODE = 'check_violation', CONSTRAINT = 'execution_identity_immutable';
    END IF;
    IF OLD.status <> 'in_progress' AND NEW.status <> OLD.status THEN
      RAISE EXCEPTION 'A submitted or cancelled execution cannot change status.'
        USING ERRCODE = 'check_violation', CONSTRAINT = 'execution_final_state';
    END IF;
  END IF;
  RETURN NEW;
END
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION responses_guard() RETURNS trigger AS $$
DECLARE
  e_id uuid;
  e_status text;
  e_version uuid;
  i_version uuid;
BEGIN
  IF TG_OP = 'DELETE' THEN e_id := OLD.execution_id; ELSE e_id := NEW.execution_id; END IF;
  SELECT status, version_id INTO e_status, e_version FROM "{ex}" WHERE id = e_id;
  IF e_status IS NOT NULL AND e_status <> 'in_progress' THEN
    RAISE EXCEPTION 'The responses of a submitted or cancelled execution cannot be changed.'
      USING ERRCODE = 'check_violation', CONSTRAINT = 'response_execution_frozen';
  END IF;
  IF TG_OP = 'DELETE' THEN RETURN OLD; END IF;
  SELECT s.version_id INTO i_version FROM "{item}" i JOIN "{section}" s ON s.id = i.section_id WHERE i.id = NEW.item_id;
  IF i_version IS DISTINCT FROM e_version THEN
    RAISE EXCEPTION 'A response must answer an item of the execution''s checklist version.'
      USING ERRCODE = 'check_violation', CONSTRAINT = 'response_item_in_version';
  END IF;
  IF TG_OP = 'UPDATE' AND (NEW.item_id <> OLD.item_id OR NEW.execution_id <> OLD.execution_id) THEN
    RAISE EXCEPTION 'The item and execution of a response never change.'
      USING ERRCODE = 'check_violation', CONSTRAINT = 'response_identity_immutable';
  END IF;
  RETURN NEW;
END
$$ LANGUAGE plpgsql;
""")
        for table, fn in ((ex, "executions_guard"), (rs, "responses_guard")):
            cursor.execute(f'DROP TRIGGER IF EXISTS {fn}_trg ON "{table}"')
            cursor.execute(f'CREATE TRIGGER {fn}_trg BEFORE INSERT OR UPDATE OR DELETE ON "{table}" FOR EACH ROW EXECUTE FUNCTION {fn}()')
    return True
