"""Database guards of the evaluation engine - the layers below the services.

* ``evaluation_guard_ruleset`` - a rule set can be inserted, changed or deleted **only while its checklist version is a draft** (the same rule,
  and the same explicit maintenance override, as the version's own definition and scoring); its version and organization never change; its
  organization equals the organization of the version's template.
* ``evaluation_guard_run`` - a run is **append-only**: no delete, and an update may only retire ``is_current`` (true -> false).  A new run must agree
  with its execution (organization, version) and rule set (same version).
* ``evaluation_guard_action`` - an action request is never deleted; only a ``pending`` request may change (to its final status, result and error),
  and its identity columns never change.  A new request must agree with its run (organization, execution).

Installed idempotently by a ``post_migrate`` handler.  Honest boundary: a database superuser or the table owner can drop triggers and ``TRUNCATE``
ignores them (see ``docs/evaluation-integrations-and-idempotency.md``).
"""

from __future__ import annotations

from django.db import connections

MAINTENANCE_SETTING = "checklists.allow_definition_change"


def _sql(t: dict[str, str]) -> list[str]:
    return [
        f"""
CREATE OR REPLACE FUNCTION evaluation_guard_ruleset() RETURNS trigger AS $$
DECLARE
  v_old text;
  v_new text;
  org uuid;
BEGIN
  IF current_setting('{MAINTENANCE_SETTING}', true) = 'on' THEN
    RETURN CASE WHEN TG_OP = 'DELETE' THEN OLD ELSE NEW END;
  END IF;
  IF TG_OP <> 'INSERT' THEN SELECT status INTO v_old FROM "{t['version']}" WHERE id = OLD.version_id; END IF;
  IF TG_OP <> 'DELETE' THEN SELECT status INTO v_new FROM "{t['version']}" WHERE id = NEW.version_id; END IF;
  IF (v_old IS NOT NULL AND v_old <> 'draft') OR (v_new IS NOT NULL AND v_new <> 'draft') THEN
    RAISE EXCEPTION 'The evaluation rules of a published checklist version cannot be changed; create a new draft version instead.'
      USING ERRCODE = 'check_violation', CONSTRAINT = 'evaluation_ruleset_immutable';
  END IF;
  IF TG_OP = 'UPDATE' AND (NEW.version_id <> OLD.version_id OR NEW.organization_id <> OLD.organization_id) THEN
    RAISE EXCEPTION 'The version and organization of an evaluation rule set never change.'
      USING ERRCODE = 'check_violation', CONSTRAINT = 'evaluation_ruleset_identity';
  END IF;
  IF TG_OP <> 'DELETE' THEN
    SELECT t.organization_id INTO org FROM "{t['version']}" v JOIN "{t['template']}" t ON t.id = v.template_id WHERE v.id = NEW.version_id;
    IF org IS DISTINCT FROM NEW.organization_id THEN
      RAISE EXCEPTION 'An evaluation rule set must belong to the organization of its checklist.'
        USING ERRCODE = 'check_violation', CONSTRAINT = 'evaluation_ruleset_organization';
    END IF;
  END IF;
  RETURN CASE WHEN TG_OP = 'DELETE' THEN OLD ELSE NEW END;
END
$$ LANGUAGE plpgsql;
""",
        f"""
CREATE OR REPLACE FUNCTION evaluation_guard_run() RETURNS trigger AS $$
DECLARE
  e_org uuid;
  e_version uuid;
  r_version uuid;
BEGIN
  IF TG_OP = 'DELETE' THEN
    RAISE EXCEPTION 'An evaluation run is part of the record and cannot be deleted.'
      USING ERRCODE = 'check_violation', CONSTRAINT = 'evaluation_run_append_only';
  END IF;
  IF TG_OP = 'UPDATE' THEN
    IF OLD.is_current AND NOT NEW.is_current AND to_jsonb(NEW) - 'is_current' = to_jsonb(OLD) - 'is_current' THEN
      RETURN NEW;
    END IF;
    RAISE EXCEPTION 'An evaluation run cannot be changed; a re-evaluation appends a new run.'
      USING ERRCODE = 'check_violation', CONSTRAINT = 'evaluation_run_append_only';
  END IF;
  SELECT organization_id, version_id INTO e_org, e_version FROM "{t['execution']}" WHERE id = NEW.execution_id;
  SELECT version_id INTO r_version FROM "{t['ruleset']}" WHERE id = NEW.ruleset_id;
  IF e_org IS DISTINCT FROM NEW.organization_id OR e_version IS DISTINCT FROM NEW.version_id OR r_version IS DISTINCT FROM NEW.version_id THEN
    RAISE EXCEPTION 'An evaluation run must agree with its execution, version, rule set and organization.'
      USING ERRCODE = 'check_violation', CONSTRAINT = 'evaluation_run_consistency';
  END IF;
  RETURN NEW;
END
$$ LANGUAGE plpgsql;
""",
        f"""
CREATE OR REPLACE FUNCTION evaluation_guard_action() RETURNS trigger AS $$
DECLARE
  r_org uuid;
  r_exec uuid;
BEGIN
  IF TG_OP = 'DELETE' THEN
    RAISE EXCEPTION 'An evaluation action request is part of the record and cannot be deleted.'
      USING ERRCODE = 'check_violation', CONSTRAINT = 'evaluation_action_append_only';
  END IF;
  IF TG_OP = 'UPDATE' THEN
    IF OLD.status <> 'pending' THEN
      RAISE EXCEPTION 'A finished action request cannot be changed.'
        USING ERRCODE = 'check_violation', CONSTRAINT = 'evaluation_action_append_only';
    END IF;
    IF NEW.run_id <> OLD.run_id OR NEW.organization_id <> OLD.organization_id OR NEW.execution_id <> OLD.execution_id OR NEW.rule_code <> OLD.rule_code
       OR NEW.action_index <> OLD.action_index OR NEW.action_type <> OLD.action_type OR NEW.action_key <> OLD.action_key THEN
      RAISE EXCEPTION 'The identity of an action request never changes.'
        USING ERRCODE = 'check_violation', CONSTRAINT = 'evaluation_action_identity';
    END IF;
    RETURN NEW;
  END IF;
  SELECT organization_id, execution_id INTO r_org, r_exec FROM "{t['run']}" WHERE id = NEW.run_id;
  IF r_org IS DISTINCT FROM NEW.organization_id OR r_exec IS DISTINCT FROM NEW.execution_id THEN
    RAISE EXCEPTION 'An action request must agree with its run.'
      USING ERRCODE = 'check_violation', CONSTRAINT = 'evaluation_action_consistency';
  END IF;
  RETURN NEW;
END
$$ LANGUAGE plpgsql;
""",
    ]


def install_database_guards(using: str = "default") -> bool:
    """Create or refresh the triggers.  Returns False (and does nothing) when the schema is not ready or not PostgreSQL."""
    from apps.checklists.models import ChecklistTemplate, ChecklistVersion
    from apps.executions.models import ChecklistExecution

    from .models import EvaluationActionRequest, EvaluationRun, EvaluationRuleSet

    connection = connections[using]
    if connection.vendor != "postgresql":
        return False
    tables = {"version": ChecklistVersion._meta.db_table, "template": ChecklistTemplate._meta.db_table, "execution": ChecklistExecution._meta.db_table,
              "ruleset": EvaluationRuleSet._meta.db_table, "run": EvaluationRun._meta.db_table, "action": EvaluationActionRequest._meta.db_table}
    with connection.cursor() as cursor:
        existing = set(connection.introspection.table_names(cursor))
        if not set(tables.values()) <= existing:
            return False
        for statement in _sql(tables):
            cursor.execute(statement)
        for kind, function in (("ruleset", "evaluation_guard_ruleset"), ("run", "evaluation_guard_run"), ("action", "evaluation_guard_action")):
            cursor.execute(f'DROP TRIGGER IF EXISTS evaluation_{kind}_guard ON "{tables[kind]}"')
            cursor.execute(f'CREATE TRIGGER evaluation_{kind}_guard BEFORE INSERT OR UPDATE OR DELETE ON "{tables[kind]}" FOR EACH ROW EXECUTE FUNCTION {function}()')
    return True
