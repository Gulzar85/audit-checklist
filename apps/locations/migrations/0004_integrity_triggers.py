"""Database-level integrity for the location hierarchy.

Some rules span rows (a parent's organization, an ancestor chain) and so cannot be
a plain ``CHECK`` constraint.  They are enforced here with triggers so that *no*
write path - the ORM, ``QuerySet.update()``, the admin, an import, hand-written
SQL, a race between two requests - can leave the tree in an invalid state:

* a location's parent must belong to the same organization;
* a location's type must belong to the same organization or be platform-defined;
* a location can never change organization;
* a location can never be moved beneath itself or one of its descendants.  The
  check takes a per-organization advisory lock first, so two concurrent moves
  (``A under B`` and ``B under A``) cannot both pass and form a cycle;
* a location type cannot be re-homed while other organizations' locations use it.

Violations raise ``check_violation`` (SQLSTATE 23514) tagged with a constraint
name, which Django surfaces as ``IntegrityError`` and the service layer turns
into a field error (``apps.locations.services._DB_RULES``).

Known limit: a *single* SQL statement that rewrites several parents at once can
only be validated row by row against the pre-statement snapshot.  The
``validate_location_tree`` command detects any such leftover.
"""

from django.db import migrations

LOCATION_FUNCTION = """
CREATE OR REPLACE FUNCTION locations_location_integrity() RETURNS trigger AS $$
DECLARE
    parent_org uuid;
    type_org uuid;
BEGIN
    IF TG_OP = 'UPDATE' AND NEW.organization_id IS DISTINCT FROM OLD.organization_id THEN
        RAISE EXCEPTION 'A location cannot be moved to another organization.'
            USING ERRCODE = '23514', CONSTRAINT = 'location_organization_immutable';
    END IF;

    IF NEW.parent_id IS NOT NULL AND NEW.parent_id = NEW.id THEN
        RAISE EXCEPTION 'A location cannot be its own parent.'
            USING ERRCODE = '23514', CONSTRAINT = 'location_cannot_be_its_own_parent';
    END IF;

    IF NEW.parent_id IS NOT NULL
       AND (TG_OP = 'INSERT' OR NEW.parent_id IS DISTINCT FROM OLD.parent_id) THEN
        SELECT organization_id INTO parent_org FROM locations_location WHERE id = NEW.parent_id;
        IF parent_org IS DISTINCT FROM NEW.organization_id THEN
            RAISE EXCEPTION 'The parent location must belong to the same organization.'
                USING ERRCODE = '23514', CONSTRAINT = 'location_parent_same_organization';
        END IF;

        IF TG_OP = 'UPDATE' THEN
            PERFORM pg_advisory_xact_lock(
                hashtextextended('locations_location:' || NEW.organization_id::text, 0)
            );
            IF EXISTS (
                WITH RECURSIVE ancestors(id, parent_id) AS (
                    SELECT id, parent_id FROM locations_location WHERE id = NEW.parent_id
                    UNION
                    SELECT l.id, l.parent_id
                      FROM locations_location l JOIN ancestors a ON l.id = a.parent_id
                )
                SELECT 1 FROM ancestors WHERE id = NEW.id
            ) THEN
                RAISE EXCEPTION 'A location cannot be moved beneath one of its own descendants.'
                    USING ERRCODE = '23514', CONSTRAINT = 'location_hierarchy_cycle';
            END IF;
        END IF;
    END IF;

    IF TG_OP = 'INSERT' OR NEW.location_type_id IS DISTINCT FROM OLD.location_type_id THEN
        SELECT organization_id INTO type_org FROM locations_locationtype WHERE id = NEW.location_type_id;
        IF type_org IS NOT NULL AND type_org IS DISTINCT FROM NEW.organization_id THEN
            RAISE EXCEPTION 'The location type must belong to the same organization or be platform-defined.'
                USING ERRCODE = '23514', CONSTRAINT = 'location_type_same_organization';
        END IF;
    END IF;

    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
"""

LOCATION_TRIGGER = """
CREATE TRIGGER locations_location_integrity_trg
    BEFORE INSERT OR UPDATE ON locations_location
    FOR EACH ROW EXECUTE FUNCTION locations_location_integrity();
"""

TYPE_FUNCTION = """
CREATE OR REPLACE FUNCTION locations_location_type_integrity() RETURNS trigger AS $$
BEGIN
    IF NEW.organization_id IS NOT NULL
       AND NEW.organization_id IS DISTINCT FROM OLD.organization_id
       AND EXISTS (
            SELECT 1 FROM locations_location
             WHERE location_type_id = NEW.id AND organization_id <> NEW.organization_id
       ) THEN
        RAISE EXCEPTION 'Locations of other organizations already use this location type.'
            USING ERRCODE = '23514', CONSTRAINT = 'location_type_organization_in_use';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
"""

TYPE_TRIGGER = """
CREATE TRIGGER locations_locationtype_integrity_trg
    BEFORE UPDATE OF organization_id ON locations_locationtype
    FOR EACH ROW EXECUTE FUNCTION locations_location_type_integrity();
"""

FORWARD = [LOCATION_FUNCTION, LOCATION_TRIGGER, TYPE_FUNCTION, TYPE_TRIGGER]

REVERSE = [
    "DROP TRIGGER IF EXISTS locations_locationtype_integrity_trg ON locations_locationtype;",
    "DROP FUNCTION IF EXISTS locations_location_type_integrity();",
    "DROP TRIGGER IF EXISTS locations_location_integrity_trg ON locations_location;",
    "DROP FUNCTION IF EXISTS locations_location_integrity();",
]


class Migration(migrations.Migration):

    dependencies = [
        ("locations", "0003_location_integrity_and_required_type"),
    ]

    operations = [
        migrations.RunSQL(FORWARD, REVERSE),
    ]
