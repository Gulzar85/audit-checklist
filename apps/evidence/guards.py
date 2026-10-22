"""PostgreSQL integrity triggers for evidence (ADR 0060) - what CHECK constraints cannot express.

* an attachment's organization equals its asset's and its execution's;
* the file facts of an asset (organization, hash, size, storage key, content type, uploader) never change after creation;
* an attachment's asset, execution, item and organization never change (detaching only fills the ``detached_*`` columns);
* neither assets nor attachments are ever deleted (evidence is part of the record: detach, do not erase).

Installed idempotently by ``post_migrate``.  Limits: a database superuser can drop the triggers and ``TRUNCATE`` ignores them.
"""

from __future__ import annotations

from django.db import connections


def install_database_guard(using: str = "default") -> bool:
    from apps.executions.models import ChecklistExecution

    from .models import EvidenceAsset, EvidenceAttachment

    connection = connections[using]
    if connection.vendor != "postgresql":
        return False
    asset, att, ex = EvidenceAsset._meta.db_table, EvidenceAttachment._meta.db_table, ChecklistExecution._meta.db_table
    with connection.cursor() as cursor:
        if not {asset, att, ex} <= set(connection.introspection.table_names(cursor)):
            return False
        cursor.execute(f"""
CREATE OR REPLACE FUNCTION evidence_asset_guard() RETURNS trigger AS $$
BEGIN
  IF TG_OP = 'DELETE' THEN
    RAISE EXCEPTION 'Evidence is part of the record and cannot be deleted.' USING ERRCODE = 'check_violation', CONSTRAINT = 'evidence_never_deleted';
  END IF;
  IF TG_OP = 'UPDATE' AND (NEW.organization_id <> OLD.organization_id OR NEW.sha256 <> OLD.sha256 OR NEW.size <> OLD.size
       OR NEW.storage_key <> OLD.storage_key OR NEW.content_type <> OLD.content_type OR NEW.uploaded_by_id IS DISTINCT FROM OLD.uploaded_by_id
       OR NEW.kind <> OLD.kind) THEN
    RAISE EXCEPTION 'The facts of a stored evidence file never change.' USING ERRCODE = 'check_violation', CONSTRAINT = 'evidence_asset_immutable';
  END IF;
  RETURN NEW;
END
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION evidence_attachment_guard() RETURNS trigger AS $$
DECLARE
  a_org uuid;
  e_org uuid;
BEGIN
  IF TG_OP = 'DELETE' THEN
    RAISE EXCEPTION 'Evidence is part of the record and cannot be deleted.' USING ERRCODE = 'check_violation', CONSTRAINT = 'evidence_never_deleted';
  END IF;
  SELECT organization_id INTO a_org FROM "{asset}" WHERE id = NEW.asset_id;
  IF a_org IS DISTINCT FROM NEW.organization_id THEN
    RAISE EXCEPTION 'An attachment must belong to its file''s organization.' USING ERRCODE = 'check_violation', CONSTRAINT = 'attachment_org_consistent';
  END IF;
  IF NEW.execution_id IS NOT NULL THEN
    SELECT organization_id INTO e_org FROM "{ex}" WHERE id = NEW.execution_id;
    IF e_org IS DISTINCT FROM NEW.organization_id THEN
      RAISE EXCEPTION 'An attachment must belong to its target''s organization.' USING ERRCODE = 'check_violation', CONSTRAINT = 'attachment_org_consistent';
    END IF;
  END IF;
  IF TG_OP = 'UPDATE' AND (NEW.asset_id <> OLD.asset_id OR NEW.organization_id <> OLD.organization_id OR NEW.execution_id IS DISTINCT FROM OLD.execution_id
       OR NEW.item_id IS DISTINCT FROM OLD.item_id OR NEW.target_type <> OLD.target_type) THEN
    RAISE EXCEPTION 'The file and target of an attachment never change.' USING ERRCODE = 'check_violation', CONSTRAINT = 'attachment_identity_immutable';
  END IF;
  IF TG_OP = 'UPDATE' AND OLD.detached_at IS NOT NULL AND NEW.detached_at IS NULL THEN
    RAISE EXCEPTION 'A detached attachment cannot be re-attached; attach the file again.' USING ERRCODE = 'check_violation', CONSTRAINT = 'attachment_identity_immutable';
  END IF;
  RETURN NEW;
END
$$ LANGUAGE plpgsql;
""")
        for table, fn, events in ((asset, "evidence_asset_guard", "UPDATE OR DELETE"), (att, "evidence_attachment_guard", "INSERT OR UPDATE OR DELETE")):
            cursor.execute(f'DROP TRIGGER IF EXISTS {fn}_trg ON "{table}"')
            cursor.execute(f'CREATE TRIGGER {fn}_trg BEFORE {events} ON "{table}" FOR EACH ROW EXECUTE FUNCTION {fn}()')
    return True
