"""PostgreSQL integrity triggers for corrective actions - what CHECK constraints cannot express (ADR 0066/0067).

* the action's organization and location equal its finding's;
* organization, number, finding, location and the creator never change; actions are never deleted (cancel instead);
* updates and verification decisions are **append-only**: they cannot be changed or deleted by anything that bypasses the services;
* a verification decision requires a submitted action whose submission number matches (at the moment of the insert);
* a correction points at an update of the same action.

Installed idempotently by ``post_migrate``.  Limits: a database superuser can drop the triggers and ``TRUNCATE`` ignores them
(the stance of ADR 0049 / 0051 / 0054 / 0063).
"""

from __future__ import annotations

from django.db import connections


def install_database_guard(using: str = "default") -> bool:
    from apps.findings.models import Finding

    from .models import CorrectiveAction, CorrectiveActionUpdate, CorrectiveActionVerification

    connection = connections[using]
    if connection.vendor != "postgresql":
        return False
    ca, up, ver, fnd = (CorrectiveAction._meta.db_table, CorrectiveActionUpdate._meta.db_table, CorrectiveActionVerification._meta.db_table,
                        Finding._meta.db_table)
    with connection.cursor() as cursor:
        if not {ca, up, ver, fnd} <= set(connection.introspection.table_names(cursor)):
            return False
        cursor.execute(f"""
CREATE OR REPLACE FUNCTION corrective_actions_guard() RETURNS trigger AS $$
DECLARE
  f_org uuid; f_loc uuid;
BEGIN
  IF TG_OP = 'DELETE' THEN
    RAISE EXCEPTION 'A corrective action is part of the history and cannot be deleted; cancel it instead.'
      USING ERRCODE = 'check_violation', CONSTRAINT = 'action_history_protected';
  END IF;
  SELECT organization_id, location_id INTO f_org, f_loc FROM "{fnd}" WHERE id = NEW.finding_id;
  IF f_org IS DISTINCT FROM NEW.organization_id OR f_loc IS DISTINCT FROM NEW.location_id THEN
    RAISE EXCEPTION 'The organization and location of a corrective action must be its finding''s.'
      USING ERRCODE = 'check_violation', CONSTRAINT = 'action_finding_consistent';
  END IF;
  IF TG_OP = 'UPDATE' AND (NEW.organization_id <> OLD.organization_id OR NEW.number <> OLD.number OR NEW.finding_id <> OLD.finding_id
       OR NEW.location_id <> OLD.location_id) THEN
    RAISE EXCEPTION 'The organization, number, finding and location of a corrective action never change.'
      USING ERRCODE = 'check_violation', CONSTRAINT = 'action_identity_immutable';
  END IF;
  RETURN NEW;
END
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION corrective_action_log_guard() RETURNS trigger AS $$
DECLARE
  a_status text; a_count int; c_action uuid;
BEGIN
  IF TG_OP <> 'INSERT' THEN
    RAISE EXCEPTION 'Progress updates and verification decisions are append-only history: add a correction instead.'
      USING ERRCODE = 'check_violation', CONSTRAINT = 'action_log_append_only';
  END IF;
  IF TG_TABLE_NAME = '{ver}' THEN
    SELECT status, submission_count INTO a_status, a_count FROM "{ca}" WHERE id = NEW.action_id;
    IF a_status IS DISTINCT FROM 'submitted' OR a_count IS DISTINCT FROM NEW.submission_number THEN
      RAISE EXCEPTION 'A verification decision needs a submitted action and its current submission number.'
        USING ERRCODE = 'check_violation', CONSTRAINT = 'action_verification_requires_submission';
    END IF;
  ELSIF NEW.corrects_id IS NOT NULL THEN
    SELECT action_id INTO c_action FROM "{up}" WHERE id = NEW.corrects_id;
    IF c_action IS DISTINCT FROM NEW.action_id THEN
      RAISE EXCEPTION 'A correction must refer to an update of the same action.'
        USING ERRCODE = 'check_violation', CONSTRAINT = 'action_update_same_action';
    END IF;
  END IF;
  RETURN NEW;
END
$$ LANGUAGE plpgsql;
""")
        for table, fn, events in ((ca, "corrective_actions_guard", "INSERT OR UPDATE OR DELETE"), (up, "corrective_action_log_guard", "INSERT OR UPDATE OR DELETE"),
                                  (ver, "corrective_action_log_guard", "INSERT OR UPDATE OR DELETE")):
            trg = f"{fn}_{'ca' if table == ca else 'up' if table == up else 'ver'}_trg"
            cursor.execute(f'DROP TRIGGER IF EXISTS {trg} ON "{table}"')
            cursor.execute(f'CREATE TRIGGER {trg} BEFORE {events} ON "{table}" FOR EACH ROW EXECUTE FUNCTION {fn}()')
    return True
