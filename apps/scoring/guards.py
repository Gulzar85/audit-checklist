"""Database guards of the scoring engine - the layers below the services.

* ``scoring_guard_config`` - a scoring configuration can be inserted, changed or deleted **only while its checklist version is a draft**
  (the same rule, and the same explicit maintenance override, as the version's own definition); its version and organization never change;
  its organization always equals the organization of the version's template.
* ``scoring_guard_result`` - a score result is **append-only**: no delete, and an update may only retire the *current* flag
  (``true -> false``) when an authorized recalculation appends its successor.  A new result must agree with its execution (organization,
  version) and with its configuration (same version).

Installed idempotently by a ``post_migrate`` handler (``ScoringConfig.ready``), so every environment has them after ``migrate``.
Honest boundary: a database superuser or the table owner can drop triggers and ``TRUNCATE`` ignores them; see ``docs/scoring-snapshots-and-offline.md``.
"""

from __future__ import annotations

from django.db import connections

MAINTENANCE_SETTING = "checklists.allow_definition_change"


def _sql(t: dict[str, str]) -> list[str]:
    return [
        f"""
CREATE OR REPLACE FUNCTION scoring_guard_config() RETURNS trigger AS $$
DECLARE
  v_old text;
  v_new text;
  org uuid;
BEGIN
  IF current_setting('{MAINTENANCE_SETTING}', true) = 'on' THEN
    RETURN CASE WHEN TG_OP = 'DELETE' THEN OLD ELSE NEW END;
  END IF;
  IF TG_OP <> 'INSERT' THEN
    SELECT status INTO v_old FROM "{t['version']}" WHERE id = OLD.version_id;
  END IF;
  IF TG_OP <> 'DELETE' THEN
    SELECT status INTO v_new FROM "{t['version']}" WHERE id = NEW.version_id;
  END IF;
  IF (v_old IS NOT NULL AND v_old <> 'draft') OR (v_new IS NOT NULL AND v_new <> 'draft') THEN
    RAISE EXCEPTION 'The scoring of a published checklist version cannot be changed; create a new draft version instead.'
      USING ERRCODE = 'check_violation', CONSTRAINT = 'scoring_config_immutable';
  END IF;
  IF TG_OP = 'UPDATE' THEN
    IF NEW.version_id <> OLD.version_id OR NEW.organization_id <> OLD.organization_id THEN
      RAISE EXCEPTION 'The version and organization of a scoring configuration never change.'
        USING ERRCODE = 'check_violation', CONSTRAINT = 'scoring_config_identity';
    END IF;
  END IF;
  IF TG_OP <> 'DELETE' THEN
    SELECT t.organization_id INTO org FROM "{t['version']}" v JOIN "{t['template']}" t ON t.id = v.template_id WHERE v.id = NEW.version_id;
    IF org IS DISTINCT FROM NEW.organization_id THEN
      RAISE EXCEPTION 'A scoring configuration must belong to the organization of its checklist.'
        USING ERRCODE = 'check_violation', CONSTRAINT = 'scoring_config_organization';
    END IF;
  END IF;
  RETURN CASE WHEN TG_OP = 'DELETE' THEN OLD ELSE NEW END;
END
$$ LANGUAGE plpgsql;
""",
        f"""
CREATE OR REPLACE FUNCTION scoring_guard_result() RETURNS trigger AS $$
DECLARE
  e_org uuid;
  e_version uuid;
  c_version uuid;
BEGIN
  IF TG_OP = 'DELETE' THEN
    RAISE EXCEPTION 'A score result is part of the record and cannot be deleted.'
      USING ERRCODE = 'check_violation', CONSTRAINT = 'score_result_append_only';
  END IF;
  IF TG_OP = 'UPDATE' THEN
    IF OLD.is_current AND NOT NEW.is_current
       AND to_jsonb(NEW) - 'is_current' = to_jsonb(OLD) - 'is_current' THEN
      RETURN NEW;
    END IF;
    RAISE EXCEPTION 'A score result cannot be changed; an authorized recalculation appends a new result.'
      USING ERRCODE = 'check_violation', CONSTRAINT = 'score_result_append_only';
  END IF;
  SELECT organization_id, version_id INTO e_org, e_version FROM "{t['execution']}" WHERE id = NEW.execution_id;
  SELECT version_id INTO c_version FROM "{t['config']}" WHERE id = NEW.config_id;
  IF e_org IS DISTINCT FROM NEW.organization_id OR e_version IS DISTINCT FROM NEW.version_id OR c_version IS DISTINCT FROM NEW.version_id THEN
    RAISE EXCEPTION 'A score result must agree with its execution, version, configuration and organization.'
      USING ERRCODE = 'check_violation', CONSTRAINT = 'score_result_consistency';
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

    from .models import ScoreResult, ScoringConfig

    connection = connections[using]
    if connection.vendor != "postgresql":
        return False
    tables = {"version": ChecklistVersion._meta.db_table, "template": ChecklistTemplate._meta.db_table, "execution": ChecklistExecution._meta.db_table,
              "config": ScoringConfig._meta.db_table, "result": ScoreResult._meta.db_table}
    with connection.cursor() as cursor:
        existing = set(connection.introspection.table_names(cursor))
        if not set(tables.values()) <= existing:
            return False
        for statement in _sql(tables):
            cursor.execute(statement)
        cursor.execute(f'DROP TRIGGER IF EXISTS scoring_config_guard ON "{tables["config"]}"')
        cursor.execute(f'CREATE TRIGGER scoring_config_guard BEFORE INSERT OR UPDATE OR DELETE ON "{tables["config"]}" FOR EACH ROW EXECUTE FUNCTION scoring_guard_config()')
        cursor.execute(f'DROP TRIGGER IF EXISTS scoring_result_guard ON "{tables["result"]}"')
        cursor.execute(f'CREATE TRIGGER scoring_result_guard BEFORE INSERT OR UPDATE OR DELETE ON "{tables["result"]}" FOR EACH ROW EXECUTE FUNCTION scoring_guard_result()')
    return True
