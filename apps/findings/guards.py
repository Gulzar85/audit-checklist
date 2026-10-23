"""PostgreSQL integrity triggers for findings - what CHECK constraints cannot express (ADR 0063/0064).

* a finding, its category, severity, location, assignment and execution all belong to one organization;
* the source traceability is consistent: the execution runs the finding's assignment and version, the location is the assignment's
  location, the item belongs to that version, the response belongs to that execution and item;
* the traceability, number, organization, source and detection identity never change after creation;
* a finding is never deleted (history); a category or severity that findings use is protected by ``PROTECT`` keys.

Installed idempotently by ``post_migrate``.  Limits: a database superuser can drop the trigger and ``TRUNCATE`` ignores it
(the stance of ADR 0049 / 0051 / 0054).  Status changes are *not* guarded here: they are validated by ``services.lifecycle``.
"""

from __future__ import annotations

from django.db import connections


def install_database_guard(using: str = "default") -> bool:
    from apps.assignments.models import ChecklistAssignment
    from apps.checklists.models import ChecklistItem, ChecklistSection
    from apps.executions.models import ChecklistExecution, ChecklistResponse
    from apps.locations.models import Location

    from .models import Finding, FindingCategory, FindingSeverity

    connection = connections[using]
    if connection.vendor != "postgresql":
        return False
    f, cat, sev = Finding._meta.db_table, FindingCategory._meta.db_table, FindingSeverity._meta.db_table
    loc, asg, ex, rs = Location._meta.db_table, ChecklistAssignment._meta.db_table, ChecklistExecution._meta.db_table, ChecklistResponse._meta.db_table
    item, section = ChecklistItem._meta.db_table, ChecklistSection._meta.db_table
    with connection.cursor() as cursor:
        if not {f, cat, sev, loc, asg, ex, rs, item, section} <= set(connection.introspection.table_names(cursor)):
            return False
        cursor.execute(f"""
CREATE OR REPLACE FUNCTION findings_guard() RETURNS trigger AS $$
DECLARE
  c_org uuid; s_org uuid; l_org uuid; a_org uuid; a_loc uuid; e_org uuid; e_asg uuid; e_ver uuid; i_ver uuid; r_exec uuid; r_item uuid;
BEGIN
  IF TG_OP = 'DELETE' THEN
    RAISE EXCEPTION 'A finding is part of the history and cannot be deleted; cancel it instead.'
      USING ERRCODE = 'check_violation', CONSTRAINT = 'finding_history_protected';
  END IF;
  SELECT organization_id INTO c_org FROM "{cat}" WHERE id = NEW.category_id;
  SELECT organization_id INTO s_org FROM "{sev}" WHERE id = NEW.severity_id;
  SELECT organization_id INTO l_org FROM "{loc}" WHERE id = NEW.location_id;
  IF c_org IS DISTINCT FROM NEW.organization_id OR s_org IS DISTINCT FROM NEW.organization_id OR l_org IS DISTINCT FROM NEW.organization_id THEN
    RAISE EXCEPTION 'The category, severity and location of a finding must belong to its organization.'
      USING ERRCODE = 'check_violation', CONSTRAINT = 'finding_organization_consistent';
  END IF;
  IF NEW.assignment_id IS NOT NULL THEN
    SELECT organization_id, location_id INTO a_org, a_loc FROM "{asg}" WHERE id = NEW.assignment_id;
    IF a_org IS DISTINCT FROM NEW.organization_id THEN
      RAISE EXCEPTION 'The assignment of a finding must belong to its organization.'
        USING ERRCODE = 'check_violation', CONSTRAINT = 'finding_organization_consistent';
    END IF;
  END IF;
  IF NEW.execution_id IS NOT NULL THEN
    SELECT organization_id, assignment_id, version_id INTO e_org, e_asg, e_ver FROM "{ex}" WHERE id = NEW.execution_id;
    IF e_org IS DISTINCT FROM NEW.organization_id OR e_asg IS DISTINCT FROM NEW.assignment_id OR e_ver IS DISTINCT FROM NEW.version_id
       OR a_loc IS DISTINCT FROM NEW.location_id THEN
      RAISE EXCEPTION 'The execution, assignment, version and location of a finding must agree.'
        USING ERRCODE = 'check_violation', CONSTRAINT = 'finding_source_consistent';
    END IF;
  END IF;
  IF NEW.item_id IS NOT NULL THEN
    SELECT s.version_id INTO i_ver FROM "{item}" i JOIN "{section}" s ON s.id = i.section_id WHERE i.id = NEW.item_id;
    IF i_ver IS DISTINCT FROM NEW.version_id THEN
      RAISE EXCEPTION 'The item of a finding must belong to the execution''s checklist version.'
        USING ERRCODE = 'check_violation', CONSTRAINT = 'finding_source_consistent';
    END IF;
  END IF;
  IF NEW.response_id IS NOT NULL THEN
    SELECT execution_id, item_id INTO r_exec, r_item FROM "{rs}" WHERE id = NEW.response_id;
    IF r_exec IS DISTINCT FROM NEW.execution_id OR r_item IS DISTINCT FROM NEW.item_id THEN
      RAISE EXCEPTION 'The response of a finding must belong to its execution and item.'
        USING ERRCODE = 'check_violation', CONSTRAINT = 'finding_source_consistent';
    END IF;
  END IF;
  IF TG_OP = 'UPDATE' THEN
    IF NEW.organization_id <> OLD.organization_id OR NEW.number <> OLD.number OR NEW.source_type <> OLD.source_type
       OR NEW.location_id <> OLD.location_id OR NEW.detection_key <> OLD.detection_key OR NEW.rule_code <> OLD.rule_code
       OR NEW.detected_at <> OLD.detected_at
       OR NEW.assignment_id IS DISTINCT FROM OLD.assignment_id OR NEW.execution_id IS DISTINCT FROM OLD.execution_id
       OR NEW.version_id IS DISTINCT FROM OLD.version_id OR NEW.item_id IS DISTINCT FROM OLD.item_id
       OR NEW.response_id IS DISTINCT FROM OLD.response_id THEN
      RAISE EXCEPTION 'The source traceability of a finding never changes.'
        USING ERRCODE = 'check_violation', CONSTRAINT = 'finding_identity_immutable';
    END IF;
  END IF;
  RETURN NEW;
END
$$ LANGUAGE plpgsql;
""")
        cursor.execute(f'DROP TRIGGER IF EXISTS findings_guard_trg ON "{f}"')
        cursor.execute(f'CREATE TRIGGER findings_guard_trg BEFORE INSERT OR UPDATE OR DELETE ON "{f}" FOR EACH ROW EXECUTE FUNCTION findings_guard()')
    return True
