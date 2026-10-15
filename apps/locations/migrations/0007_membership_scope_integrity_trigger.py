"""A membership scope must point at a location of the membership's own organization.

The service and ``clean`` check it; this trigger is the safety net for any other write path
(admin, shell, a future importer).  The location row is locked ``FOR SHARE`` so a concurrent
re-homing cannot slip past, mirroring the Phase 1 location triggers.
"""

from django.db import migrations

FORWARD = """
CREATE OR REPLACE FUNCTION locations_membership_scope_integrity() RETURNS trigger AS $$
DECLARE
    membership_org uuid;
    location_org uuid;
BEGIN
    SELECT organization_id INTO membership_org
        FROM organizations_organizationmembership WHERE id = NEW.membership_id;
    SELECT organization_id INTO location_org
        FROM locations_location WHERE id = NEW.location_id FOR SHARE;
    IF membership_org IS DISTINCT FROM location_org THEN
        RAISE EXCEPTION 'membership_scope_same_organization'
            USING ERRCODE = '23514', CONSTRAINT = 'membership_scope_same_organization';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER locations_membership_scope_integrity_trg
    BEFORE INSERT OR UPDATE OF membership_id, location_id ON locations_membershiplocationscope
    FOR EACH ROW EXECUTE FUNCTION locations_membership_scope_integrity();
"""

REVERSE = """
DROP TRIGGER IF EXISTS locations_membership_scope_integrity_trg ON locations_membershiplocationscope;
DROP FUNCTION IF EXISTS locations_membership_scope_integrity();
"""


class Migration(migrations.Migration):

    dependencies = [("locations", "0006_membership_location_scope")]

    operations = [migrations.RunSQL(FORWARD, REVERSE)]
