"""Close a race between the location trigger and the location-type trigger.

The location trigger read a type's organization without locking the type row, while the
type trigger checked locations without locking them; run concurrently under READ
COMMITTED both could pass and leave a cross-organization type.  Taking
``FOR SHARE`` on the type row makes a concurrent re-homing of that type wait.
"""

from django.db import migrations

OLD = "SELECT organization_id INTO type_org FROM locations_locationtype WHERE id = NEW.location_type_id;"
NEW = "SELECT organization_id INTO type_org FROM locations_locationtype WHERE id = NEW.location_type_id FOR SHARE;"

SQL_FORWARD = """
DO $$
DECLARE src text;
BEGIN
    SELECT pg_get_functiondef('locations_location_integrity'::regproc) INTO src;
    src := replace(src, '{old}', '{new}');
    EXECUTE src;
END $$;
""".format(old=OLD, new=NEW)

SQL_REVERSE = """
DO $$
DECLARE src text;
BEGIN
    SELECT pg_get_functiondef('locations_location_integrity'::regproc) INTO src;
    src := replace(src, '{new}', '{old}');
    EXECUTE src;
END $$;
""".format(old=OLD, new=NEW)


class Migration(migrations.Migration):

    dependencies = [("locations", "0004_integrity_triggers")]

    operations = [migrations.RunSQL([SQL_FORWARD], [SQL_REVERSE])]
