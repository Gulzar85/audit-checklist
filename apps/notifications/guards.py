"""Database guards of the notifications engine - the layers below the services.

* ``notifications_guard_event``: an event's identity never changes; its state fields may; it is only removed by the retention command.
* ``notifications_guard_notification``: a notification is what the recipient was told - only ``is_read`` / ``read_at`` / ``archived_at`` (and the in-app visibility flag) change;
  organization, recipient, event, text and target never do.
* ``notifications_guard_delivery``: identity columns never change and **the state machine is enforced** (``constants.TRANSITIONS``: e.g. ``sent -> delivered`` only; a
  ``failed`` delivery can only go back to ``pending`` through the controlled manual retry); a delivery must agree with its notification (organization, channel).
* ``notifications_guard_attempt``: attempts are append-only.

Deletes are refused everywhere unless the retention command raised the transaction-local setting ``notifications.purge`` (``services.retention``).  Honest boundary:
a database superuser or the table owner can drop the triggers, and ``TRUNCATE`` ignores them.
"""

from __future__ import annotations

from django.db import connections

from . import constants as k


def _allowed_pairs() -> str:
    pairs = [(old, new) for old, targets in k.TRANSITIONS.items() for new in targets]
    return ", ".join(f"('{old}','{new}')" for old, new in pairs)


def _sql(t: dict[str, str]) -> list[str]:
    purge = k.PURGE_SETTING
    return [
        f"""
CREATE OR REPLACE FUNCTION notifications_guard_event() RETURNS trigger AS $$
BEGIN
  IF TG_OP = 'DELETE' THEN
    IF current_setting('{purge}', true) = 'on' THEN RETURN OLD; END IF;
    RAISE EXCEPTION 'A notification event is part of the record and cannot be deleted.' USING ERRCODE = 'check_violation', CONSTRAINT = 'notification_event_append_only';
  END IF;
  IF TG_OP = 'UPDATE' AND (NEW.organization_id <> OLD.organization_id OR NEW.event_type <> OLD.event_type OR NEW.source_type <> OLD.source_type
     OR NEW.source_id <> OLD.source_id OR NEW.dedupe_key <> OLD.dedupe_key OR NEW.occurred_at <> OLD.occurred_at OR NEW.metadata <> OLD.metadata) THEN
    RAISE EXCEPTION 'The identity of a notification event never changes.' USING ERRCODE = 'check_violation', CONSTRAINT = 'notification_event_identity';
  END IF;
  RETURN NEW;
END
$$ LANGUAGE plpgsql;
""",
        f"""
CREATE OR REPLACE FUNCTION notifications_guard_notification() RETURNS trigger AS $$
DECLARE
  ev_org uuid;
BEGIN
  IF TG_OP = 'DELETE' THEN
    IF current_setting('{purge}', true) = 'on' THEN RETURN OLD; END IF;
    RAISE EXCEPTION 'A notification cannot be deleted; it is archived or removed by the retention policy.' USING ERRCODE = 'check_violation', CONSTRAINT = 'notification_append_only';
  END IF;
  IF TG_OP = 'UPDATE' THEN
    IF NEW.organization_id <> OLD.organization_id OR NEW.recipient_id <> OLD.recipient_id OR NEW.event_id <> OLD.event_id OR NEW.title <> OLD.title
       OR NEW.message <> OLD.message OR NEW.target_type <> OLD.target_type OR NEW.target_id <> OLD.target_id OR NEW.idempotency_key <> OLD.idempotency_key
       OR NEW.category <> OLD.category OR NEW.event_type <> OLD.event_type THEN
      RAISE EXCEPTION 'A notification cannot be changed; only its read / archived state can.' USING ERRCODE = 'check_violation', CONSTRAINT = 'notification_identity';
    END IF;
    RETURN NEW;
  END IF;
  SELECT organization_id INTO ev_org FROM "{t['event']}" WHERE id = NEW.event_id;
  IF ev_org IS DISTINCT FROM NEW.organization_id THEN
    RAISE EXCEPTION 'A notification must belong to the organization of its event.' USING ERRCODE = 'check_violation', CONSTRAINT = 'notification_consistency';
  END IF;
  RETURN NEW;
END
$$ LANGUAGE plpgsql;
""",
        f"""
CREATE OR REPLACE FUNCTION notifications_guard_delivery() RETURNS trigger AS $$
DECLARE
  n_org uuid;
BEGIN
  IF TG_OP = 'DELETE' THEN
    IF current_setting('{purge}', true) = 'on' THEN RETURN OLD; END IF;
    RAISE EXCEPTION 'A delivery is part of the record and cannot be deleted.' USING ERRCODE = 'check_violation', CONSTRAINT = 'notification_delivery_append_only';
  END IF;
  IF TG_OP = 'UPDATE' THEN
    IF NEW.organization_id <> OLD.organization_id OR NEW.notification_id <> OLD.notification_id OR NEW.channel <> OLD.channel OR NEW.idempotency_key <> OLD.idempotency_key THEN
      RAISE EXCEPTION 'The identity of a delivery never changes.' USING ERRCODE = 'check_violation', CONSTRAINT = 'notification_delivery_identity';
    END IF;
    IF NEW.status <> OLD.status AND (OLD.status, NEW.status) NOT IN ({_allowed_pairs()}) THEN
      RAISE EXCEPTION 'The delivery cannot move from % to %.', OLD.status, NEW.status USING ERRCODE = 'check_violation', CONSTRAINT = 'notification_delivery_transition';
    END IF;
    RETURN NEW;
  END IF;
  SELECT organization_id INTO n_org FROM "{t['notification']}" WHERE id = NEW.notification_id;
  IF n_org IS DISTINCT FROM NEW.organization_id THEN
    RAISE EXCEPTION 'A delivery must belong to the organization of its notification.' USING ERRCODE = 'check_violation', CONSTRAINT = 'notification_delivery_consistency';
  END IF;
  RETURN NEW;
END
$$ LANGUAGE plpgsql;
""",
        f"""
CREATE OR REPLACE FUNCTION notifications_guard_attempt() RETURNS trigger AS $$
BEGIN
  IF TG_OP = 'DELETE' AND current_setting('{purge}', true) = 'on' THEN RETURN OLD; END IF;
  IF TG_OP = 'INSERT' THEN RETURN NEW; END IF;
  RAISE EXCEPTION 'A delivery attempt is append-only.' USING ERRCODE = 'check_violation', CONSTRAINT = 'delivery_attempt_append_only';
END
$$ LANGUAGE plpgsql;
""",
    ]


def install_database_guards(using: str = "default") -> bool:
    """Create or refresh the triggers.  Returns False (and does nothing) when the schema is not ready or not PostgreSQL."""
    from .models import DeliveryAttempt, Notification, NotificationDelivery, NotificationEvent

    connection = connections[using]
    if connection.vendor != "postgresql":
        return False
    tables = {"event": NotificationEvent._meta.db_table, "notification": Notification._meta.db_table, "delivery": NotificationDelivery._meta.db_table,
              "attempt": DeliveryAttempt._meta.db_table}
    with connection.cursor() as cursor:
        existing = set(connection.introspection.table_names(cursor))
        if not set(tables.values()) <= existing:
            return False
        for statement in _sql(tables):
            cursor.execute(statement)
        for kind in ("event", "notification", "delivery", "attempt"):
            cursor.execute(f'DROP TRIGGER IF EXISTS notifications_{kind}_guard ON "{tables[kind]}"')
            cursor.execute(f'CREATE TRIGGER notifications_{kind}_guard BEFORE INSERT OR UPDATE OR DELETE ON "{tables[kind]}" FOR EACH ROW EXECUTE FUNCTION notifications_guard_{kind}()')
    return True
