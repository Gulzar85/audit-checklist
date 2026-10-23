"""PostgreSQL integrity triggers for workflows - what CHECK constraints cannot express (ADR 0069/0071).

* **Published versions are immutable**: once a version leaves ``draft`` only its status may move forward (published -> superseded/archived,
  superseded -> archived); every other column is frozen.  Its states and transitions can no longer be inserted, changed or deleted - not through
  the API, the admin, an import, a child-object edit or a queryset ``update()``.  Versions are never deleted.
* A transition connects two states of *its own* version.
* An instance starts only on a published version of its definition, in a state of that version; organization, definition, version, target and
  location never change; the definition belongs to the instance's organization (or is a system definition); instances are never deleted.
* Events are append-only; tasks belong to a state of their instance's version and are never deleted.

Installed idempotently by ``post_migrate``.  Limits: a database superuser can drop the triggers and ``TRUNCATE`` ignores them.
"""

from __future__ import annotations

from django.db import connections


def install_database_guard(using: str = "default") -> bool:
    from apps.locations.models import Location

    from .models import WorkflowDefinition, WorkflowEvent, WorkflowInstance, WorkflowState, WorkflowTask, WorkflowTransition, WorkflowVersion

    connection = connections[using]
    if connection.vendor != "postgresql":
        return False
    d, v, s, t, i, k, e = (WorkflowDefinition._meta.db_table, WorkflowVersion._meta.db_table, WorkflowState._meta.db_table, WorkflowTransition._meta.db_table,
                           WorkflowInstance._meta.db_table, WorkflowTask._meta.db_table, WorkflowEvent._meta.db_table)
    loc = Location._meta.db_table
    with connection.cursor() as cursor:
        if not {d, v, s, t, i, k, e, loc} <= set(connection.introspection.table_names(cursor)):
            return False
        cursor.execute(f"""
CREATE OR REPLACE FUNCTION workflow_version_guard() RETURNS trigger AS $$
BEGIN
  IF TG_OP = 'DELETE' THEN
    RAISE EXCEPTION 'A workflow version is part of the history and cannot be deleted; archive it instead.'
      USING ERRCODE = 'check_violation', CONSTRAINT = 'workflow_version_history_protected';
  END IF;
  IF TG_OP = 'INSERT' THEN
    IF NEW.status <> 'draft' THEN
      RAISE EXCEPTION 'A workflow version is created as a draft and published through the publication service.'
        USING ERRCODE = 'check_violation', CONSTRAINT = 'workflow_version_starts_as_draft';
    END IF;
    RETURN NEW;
  END IF;
  IF OLD.status <> 'draft' THEN
    IF NEW.definition_id <> OLD.definition_id OR NEW.number <> OLD.number OR NEW.change_summary <> OLD.change_summary
       OR NEW.start_condition IS DISTINCT FROM OLD.start_condition OR NEW.allow_loops <> OLD.allow_loops OR NEW.config_hash <> OLD.config_hash
       OR NEW.published_at IS DISTINCT FROM OLD.published_at OR NEW.published_by_id IS DISTINCT FROM OLD.published_by_id THEN
      RAISE EXCEPTION 'A published workflow version is immutable; create a new draft version instead.'
        USING ERRCODE = 'check_violation', CONSTRAINT = 'workflow_version_immutable';
    END IF;
    IF NOT ((OLD.status = 'published' AND NEW.status IN ('published', 'superseded', 'archived'))
         OR (OLD.status = 'superseded' AND NEW.status IN ('superseded', 'archived'))
         OR (OLD.status = 'archived' AND NEW.status = 'archived')) THEN
      RAISE EXCEPTION 'That workflow version status change is not allowed.'
        USING ERRCODE = 'check_violation', CONSTRAINT = 'workflow_version_status_flow';
    END IF;
  ELSIF NEW.definition_id <> OLD.definition_id OR NEW.number <> OLD.number THEN
    RAISE EXCEPTION 'The definition and number of a workflow version never change.'
      USING ERRCODE = 'check_violation', CONSTRAINT = 'workflow_version_identity_immutable';
  END IF;
  RETURN NEW;
END
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION workflow_child_guard() RETURNS trigger AS $$
DECLARE
  v_id uuid; v_status text; s_ver uuid; t_ver uuid;
BEGIN
  IF TG_OP = 'DELETE' THEN v_id := OLD.version_id; ELSE v_id := NEW.version_id; END IF;
  SELECT status INTO v_status FROM "{v}" WHERE id = v_id;
  IF v_status IS DISTINCT FROM 'draft' THEN
    RAISE EXCEPTION 'The states and transitions of a published workflow version are immutable; create a new draft version instead.'
      USING ERRCODE = 'check_violation', CONSTRAINT = 'workflow_published_config_immutable';
  END IF;
  IF TG_TABLE_NAME = '{t}' AND TG_OP <> 'DELETE' THEN
    SELECT version_id INTO s_ver FROM "{s}" WHERE id = NEW.source_id;
    SELECT version_id INTO t_ver FROM "{s}" WHERE id = NEW.target_id;
    IF s_ver IS DISTINCT FROM NEW.version_id OR t_ver IS DISTINCT FROM NEW.version_id THEN
      RAISE EXCEPTION 'A transition connects two states of its own workflow version.'
        USING ERRCODE = 'check_violation', CONSTRAINT = 'workflow_transition_same_version';
    END IF;
  END IF;
  IF TG_OP = 'DELETE' THEN RETURN OLD; END IF;
  RETURN NEW;
END
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION workflow_instance_guard() RETURNS trigger AS $$
DECLARE
  v_def uuid; v_status text; d_org uuid; s_ver uuid; l_org uuid;
BEGIN
  IF TG_OP = 'DELETE' THEN
    RAISE EXCEPTION 'A workflow instance is part of the history and cannot be deleted; cancel it instead.'
      USING ERRCODE = 'check_violation', CONSTRAINT = 'workflow_instance_history_protected';
  END IF;
  SELECT definition_id, status INTO v_def, v_status FROM "{v}" WHERE id = NEW.version_id;
  SELECT organization_id INTO d_org FROM "{d}" WHERE id = NEW.definition_id;
  SELECT version_id INTO s_ver FROM "{s}" WHERE id = NEW.state_id;
  SELECT organization_id INTO l_org FROM "{loc}" WHERE id = NEW.location_id;
  IF v_def IS DISTINCT FROM NEW.definition_id OR s_ver IS DISTINCT FROM NEW.version_id THEN
    RAISE EXCEPTION 'The version and state of a workflow instance must belong to its definition.'
      USING ERRCODE = 'check_violation', CONSTRAINT = 'workflow_instance_consistent';
  END IF;
  IF (d_org IS NOT NULL AND d_org <> NEW.organization_id) OR l_org IS DISTINCT FROM NEW.organization_id THEN
    RAISE EXCEPTION 'The definition and location of a workflow instance must belong to its organization.'
      USING ERRCODE = 'check_violation', CONSTRAINT = 'workflow_instance_organization_consistent';
  END IF;
  IF TG_OP = 'INSERT' AND v_status <> 'published' THEN
    RAISE EXCEPTION 'A workflow can only start on a published version.'
      USING ERRCODE = 'check_violation', CONSTRAINT = 'workflow_instance_published_only';
  END IF;
  IF TG_OP = 'UPDATE' AND (NEW.organization_id <> OLD.organization_id OR NEW.definition_id <> OLD.definition_id OR NEW.version_id <> OLD.version_id
       OR NEW.target_type <> OLD.target_type OR NEW.target_id <> OLD.target_id OR NEW.location_id <> OLD.location_id OR NEW.number <> OLD.number) THEN
    RAISE EXCEPTION 'The organization, definition, version, target and location of a workflow instance never change.'
      USING ERRCODE = 'check_violation', CONSTRAINT = 'workflow_instance_identity_immutable';
  END IF;
  RETURN NEW;
END
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION workflow_task_guard() RETURNS trigger AS $$
DECLARE
  i_ver uuid; s_ver uuid;
BEGIN
  IF TG_OP = 'DELETE' THEN
    RAISE EXCEPTION 'A workflow task is part of the history and cannot be deleted.'
      USING ERRCODE = 'check_violation', CONSTRAINT = 'workflow_task_history_protected';
  END IF;
  SELECT version_id INTO i_ver FROM "{i}" WHERE id = NEW.instance_id;
  SELECT version_id INTO s_ver FROM "{s}" WHERE id = NEW.state_id;
  IF i_ver IS DISTINCT FROM s_ver THEN
    RAISE EXCEPTION 'A task belongs to a state of its workflow instance''s version.'
      USING ERRCODE = 'check_violation', CONSTRAINT = 'workflow_task_consistent';
  END IF;
  IF TG_OP = 'UPDATE' AND (NEW.instance_id <> OLD.instance_id OR NEW.state_id <> OLD.state_id OR NEW.entry_seq <> OLD.entry_seq OR NEW.slot <> OLD.slot) THEN
    RAISE EXCEPTION 'The instance, state and slot of a workflow task never change.'
      USING ERRCODE = 'check_violation', CONSTRAINT = 'workflow_task_identity_immutable';
  END IF;
  RETURN NEW;
END
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION workflow_event_guard() RETURNS trigger AS $$
BEGIN
  RAISE EXCEPTION 'Workflow events are append-only history.'
    USING ERRCODE = 'check_violation', CONSTRAINT = 'workflow_event_append_only';
END
$$ LANGUAGE plpgsql;
""")
        for table, fn, events in ((v, "workflow_version_guard", "INSERT OR UPDATE OR DELETE"), (s, "workflow_child_guard", "INSERT OR UPDATE OR DELETE"),
                                  (t, "workflow_child_guard", "INSERT OR UPDATE OR DELETE"), (i, "workflow_instance_guard", "INSERT OR UPDATE OR DELETE"),
                                  (k, "workflow_task_guard", "INSERT OR UPDATE OR DELETE"), (e, "workflow_event_guard", "UPDATE OR DELETE")):
            trg = f"{fn}_{table}_trg"[:60]
            cursor.execute(f'DROP TRIGGER IF EXISTS {trg} ON "{table}"')
            cursor.execute(f'CREATE TRIGGER {trg} BEFORE {events} ON "{table}" FOR EACH ROW EXECUTE FUNCTION {fn}()')
    return True
