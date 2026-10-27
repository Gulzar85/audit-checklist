"""The notifications engine with the real lifecycle (Phase 17): events, rules, recipients, preferences, templates, in-app, email delivery, retries, idempotency, guards."""

from __future__ import annotations

import threading
import uuid
from datetime import timedelta

import pytest
from django.core import mail
from django.core.exceptions import PermissionDenied
from django.db import IntegrityError, connection, transaction
from django.utils import timezone

from apps.core.models import ActivityLog
from apps.notifications import constants as k
from apps.notifications import selectors
from apps.notifications import services as ns
from apps.notifications.models import (
    DeliveryAttempt, Notification, NotificationDelivery, NotificationEvent, NotificationRule, NotificationTemplate, UserNotificationPreference,
)
from apps.notifications.services import config

from actions_support import make_action, to_status
from notification_support import SCRIPTED, ScriptedProvider, build_notification_world, permanent, transient


@pytest.fixture
def w(org_a, org_b, make_user, assign, make_type):
    return build_notification_world(org_a, org_b, make_user, assign, make_type)


def fire(w, event_type, source, actor=None, disc="t1", dispatch=False):
    event = ns.publish(event_type, source, actor=actor, discriminator=disc, process=False)
    return event, ns.process_event(event.pk, dispatch=dispatch)


@pytest.fixture
def action(w):
    return make_action(w)  # owned by worker1, created by the manager


def default_rule(w, event_type="corrective_action.assigned"):
    return NotificationRule.objects.get(organization=w["org_a"], code=config.default_rule_code(event_type))


def delivery(notification, channel):
    return NotificationDelivery.objects.get(notification=notification, channel=channel)


# ---------------------------------------------------------------------------------------------------------------- publishing
@pytest.mark.django_db
class TestPublishing:
    def test_a_known_event_is_recorded_once_per_key(self, w, action):
        a = ns.publish("corrective_action.assigned", action, actor=w["manager"], discriminator="x", process=False)
        b = ns.publish("corrective_action.assigned", action, actor=w["manager"], discriminator="x", process=False)
        assert a.pk == b.pk and NotificationEvent.objects.count() == 1
        assert (a.event_type, a.source_type, a.source_id, a.organization_id, a.status) == ("corrective_action.assigned", "corrective_action", action.pk, w["org_a"].pk, "pending")
        assert ns.publish("corrective_action.assigned", action, discriminator="y", process=False).pk != a.pk  # a genuinely new occurrence

    def test_unknown_events_and_wrong_sources_are_refused(self, w, action):
        with pytest.raises(ns.NotificationError) as info:
            ns.publish("nope.nothing", action)
        assert info.value.code == "unknown_event"
        with pytest.raises(ns.NotificationError) as info:
            ns.publish("finding.raised", action)  # a corrective action is not a finding
        assert info.value.code == "invalid_source"
        with pytest.raises(ns.NotificationError):
            ns.publish("corrective_action.assigned", "not-a-model")

    @pytest.mark.parametrize("metadata", [{"a b": 1}, {"k": ["list"]}, {"k": "x" * 101}, {f"k{i}": 1 for i in range(11)}, {"k": {"nested": 1}}, "text"])
    def test_event_metadata_is_minimal_and_validated(self, w, action, metadata):
        with pytest.raises(ns.NotificationError) as info:
            ns.publish("corrective_action.assigned", action, metadata=metadata, process=False)
        assert info.value.code == "invalid_metadata"

    def test_processing_is_scheduled_after_commit_and_a_rolled_back_change_notifies_nobody(self, w, action, django_capture_on_commit_callbacks):
        with django_capture_on_commit_callbacks(execute=True):
            ns.publish("corrective_action.assigned", action, actor=w["manager"], discriminator="ok")
        assert Notification.objects.filter(recipient=w["worker1"]).count() == 1
        with django_capture_on_commit_callbacks(execute=True) as callbacks:
            with pytest.raises(RuntimeError), transaction.atomic():
                ns.publish("corrective_action.assigned", action, actor=w["manager"], discriminator="rolled-back")
                raise RuntimeError("the business change failed")
        assert not callbacks and not NotificationEvent.objects.filter(dedupe_key__endswith="rolled-back").exists()  # the outbox row rolled back with it
        assert Notification.objects.count() == 1

    def test_the_event_is_processed_once_even_if_processed_twice(self, w, action):
        event, first = fire(w, "corrective_action.assigned", action, w["manager"])
        again = ns.process_event(event.pk, dispatch=False)
        assert first["processed"] and not again["processed"] and Notification.objects.count() == 1
        event.refresh_from_db()
        assert event.status == "processed" and event.attempts == 1 and event.processed_at

    def test_process_pending_is_bounded_and_idempotent(self, w, action):
        for i in range(3):
            ns.publish("corrective_action.assigned", action, actor=w["manager"], discriminator=f"p{i}", process=False)
        assert ns.process_pending(2, dispatch=False) == {"found": 2, "processed": 2, "failed": 0}
        assert ns.process_pending(10, dispatch=False)["processed"] == 1 and ns.process_pending(10, dispatch=False)["found"] == 0

    def test_a_failing_event_is_recorded_and_retried_without_touching_anything_else(self, w, action, monkeypatch):
        from apps.notifications.services import events

        event = ns.publish("corrective_action.assigned", action, actor=w["manager"], discriminator="boom", process=False)
        real = events._create_notifications
        monkeypatch.setattr(events, "_create_notifications", lambda ev: (_ for _ in ()).throw(RuntimeError("secret detail")))
        assert ns.process_event(event.pk, dispatch=False)["processed"] is False
        event.refresh_from_db()
        assert event.status == "failed" and event.last_error == "RuntimeError" and "secret" not in event.last_error
        assert ActivityLog.objects.filter(verb="notification.event_failed").count() == 1 and not Notification.objects.exists()
        monkeypatch.setattr(events, "_create_notifications", real)
        assert ns.process_pending(5, dispatch=False)["processed"] == 1 and Notification.objects.count() == 1


# ---------------------------------------------------------------------------------------------------------------- rules and recipients
@pytest.mark.django_db
class TestRulesAndRecipients:
    def test_the_default_rule_notifies_the_assignee_in_app_and_by_email(self, w, action):
        _event, out = fire(w, "corrective_action.assigned", action, w["manager"])
        n = Notification.objects.get()
        assert out == {"processed": True, "notifications": 1, "deliveries": 1, "note": ""}
        assert (n.recipient_id, n.organization_id, n.category, n.event_type, n.target_type, n.target_id) == (w["worker1"].pk, w["org_a"].pk, "corrective_actions", "corrective_action.assigned", "corrective_action", action.pk)
        assert n.title == "Corrective action assigned" and "Repair the equipment" in n.message and n.rule_code == "default-corrective-action-assigned"
        assert delivery(n, "in_app").status == "delivered" and delivery(n, "email").status == "pending"
        assert not hasattr(n, "url") and "http" not in n.message  # a destination is computed from the registered object, never stored

    def test_default_rules_are_created_lazily_edited_in_place_and_never_recreated(self, w, action):
        assert not NotificationRule.objects.exists()
        fire(w, "corrective_action.assigned", action, w["manager"])
        rule = default_rule(w)
        assert rule.is_system_default and rule.enabled and rule.precedence == 1000
        ns.set_rule_enabled(rule, actor=w["notifier"], enabled=False)
        fire(w, "corrective_action.assigned", action, w["manager"], disc="t2")
        assert NotificationRule.objects.filter(code=rule.code).count() == 1 and Notification.objects.count() == 1  # disabled: nothing new, and not recreated

    def test_a_disabled_rule_is_a_draft_that_never_applies(self, w, action):
        ns.seed_defaults(w["org_a"])
        ns.set_rule_enabled(default_rule(w), actor=w["notifier"], enabled=False)
        _e, out = fire(w, "corrective_action.assigned", action, w["manager"])
        assert out["notifications"] == 0 and not Notification.objects.exists()

    def test_rules_combine_their_channels_and_the_lowest_precedence_number_wins_the_rest(self, w, action):
        ns.seed_defaults(w["org_a"])
        ns.save_rule(w["org_a"], actor=w["notifier"], code="loud", data={"name": "Loud", "event_type": "corrective_action.assigned", "precedence": 10, "channels": ["in_app"],
                                                                      "recipient_strategies": [{"type": "assignee"}], "priority": "high"})
        _e, out = fire(w, "corrective_action.assigned", action, w["manager"])
        n = Notification.objects.get()  # one notification per person per event, whatever the number of rules
        assert out["notifications"] == 1 and n.rule_code == "loud" and n.priority == "high"  # precedence 10 beats the default's 1000
        assert delivery(n, "in_app") and delivery(n, "email")  # the default rule's email channel is combined in
        assert n.preference_policy == "optional"  # policy comes from the winning rule

    def test_filters_decide_whether_a_rule_applies(self, w, action):
        ns.seed_defaults(w["org_a"])
        ns.set_rule_enabled(default_rule(w), actor=w["notifier"], enabled=False)
        ns.save_rule(w["org_a"], actor=w["notifier"], code="urgent-only", data={"name": "Urgent only", "event_type": "corrective_action.assigned", "channels": ["in_app"],
                                                                              "recipient_strategies": [{"type": "assignee"}], "filters": [{"attribute": "priority", "operator": "EQUALS", "value": "urgent"}]})
        _e, out = fire(w, "corrective_action.assigned", action, w["manager"])
        assert out["notifications"] == 0  # the action is not urgent

    def test_permission_strategies_respect_location_scope_and_organization(self, w, org_b, make_user, assign):
        ca = to_status(w, make_action(w), "submitted")
        _e, out = fire(w, "corrective_action.submitted", ca, w["worker1"])
        got = {n.recipient_id for n in Notification.objects.all()}
        assert w["verifier"].pk in got and w["worker1"].pk not in got  # holds corrective_action.verify AT this location; the actor is excluded
        assert w["r2_actor"].pk not in got  # verify permission, but only at another location
        assert w["b_admin"].pk not in got and w["no_notif"].pk not in got  # another organization / no permission

    def test_the_actor_is_excluded_unless_the_rule_says_otherwise(self, w, action):
        ns.seed_defaults(w["org_a"])
        _e, _o = fire(w, "corrective_action.assigned", action, actor=w["worker1"])  # the owner assigned it to themselves
        assert not Notification.objects.exists()
        ns.save_rule(w["org_a"], actor=w["notifier"], rule=default_rule(w), data={"exclude_actor": False})
        fire(w, "corrective_action.assigned", action, actor=w["worker1"], disc="t2")
        assert Notification.objects.filter(recipient=w["worker1"]).count() == 1

    def test_inactive_users_and_users_who_cannot_open_the_object_are_never_notified(self, w, action):
        ns.seed_defaults(w["org_a"])
        ns.save_rule(w["org_a"], actor=w["notifier"], code="named", data={"name": "Named", "event_type": "corrective_action.assigned", "channels": ["in_app"],
                                                                         "recipient_strategies": [{"type": "user", "user": str(w["no_notif"].pk)}, {"type": "user", "user": str(w["aviewer"].pk)}]})
        fire(w, "corrective_action.assigned", action, w["manager"])
        got = {n.recipient_id for n in Notification.objects.all()}
        assert w["no_notif"].pk not in got  # no corrective_action.view: receiving a notification must not leak the object
        w["worker1"].status = "suspended"
        w["worker1"].save(update_fields=["status"])
        fire(w, "corrective_action.assigned", action, w["manager"], disc="t2")
        assert w["worker1"].pk not in {n.recipient_id for n in Notification.objects.filter(event__dedupe_key__endswith="t2")}

    def test_recipients_are_deduplicated_across_strategies(self, w, action):
        ns.seed_defaults(w["org_a"])
        ns.save_rule(w["org_a"], actor=w["notifier"], rule=default_rule(w), data={"recipient_strategies": [{"type": "assignee"}, {"type": "assignee"}, {"type": "permission", "permission": "corrective_action.start"}]})
        fire(w, "corrective_action.assigned", action, w["manager"])
        recipients = [n.recipient_id for n in Notification.objects.all()]
        assert len(recipients) == len(set(recipients)) and w["worker1"].pk in recipients

    def test_rule_validation(self, w):
        org, who = w["org_a"], w["notifier"]
        base = {"name": "R", "event_type": "finding.raised", "channels": ["in_app"], "recipient_strategies": [{"type": "assignee"}]}
        for bad in ({"event_type": "nope"}, {"channels": []}, {"channels": ["sms"]}, {"recipient_strategies": []}, {"recipient_strategies": [{"type": "email", "address": "a@b.c"}]},
                    {"recipient_strategies": [{"type": "permission", "permission": "not.registered"}]}, {"recipient_strategies": [{"type": "user", "user": "abc"}]},
                    {"filters": [{"attribute": "password", "operator": "EQUALS", "value": "x"}]}, {"priority": "urgent"}, {"preference_policy": "forced"}, {"precedence": -1},
                    {"template_code": "missing"}, {"name": ""}, {"enabled": "yes"}):
            with pytest.raises(ns.NotificationError):
                ns.save_rule(org, actor=who, code="r1", data={**base, **bad})
        with pytest.raises(ns.NotificationError):
            ns.save_rule(org, actor=who, code="r1", data={**base, "model": "auth.User"})  # unknown fields (no arbitrary model / field references)
        with pytest.raises(ns.NotificationError):
            ns.save_rule(org, actor=who, code="bad code!", data=base)
        assert not NotificationRule.objects.filter(code="r1").exists()

    def test_rule_changes_are_audited_and_versioned(self, w):
        rule = ns.save_rule(w["org_a"], actor=w["notifier"], code="r1", data={"name": "R", "event_type": "finding.raised", "channels": ["in_app"], "recipient_strategies": [{"type": "assignee"}]})
        assert rule.revision == 1
        rule = ns.save_rule(w["org_a"], actor=w["notifier"], rule=rule, data={"priority": "high"})
        assert rule.revision == 2 and rule.history.count() == 2
        assert ActivityLog.objects.filter(verb="notification.rule_saved", metadata__rule="r1").count() == 2
        with pytest.raises(ns.NotificationConflict):
            ns.save_rule(w["org_a"], actor=w["notifier"], code="r1", data={"name": "R", "event_type": "finding.raised", "channels": ["in_app"], "recipient_strategies": [{"type": "assignee"}]})

    def test_rule_administration_needs_permission_and_stays_inside_the_organization(self, w, org_b, make_user):
        data = {"name": "R", "event_type": "finding.raised", "channels": ["in_app"], "recipient_strategies": [{"type": "assignee"}]}
        for who in ("viewer_only", "no_notif", "worker1", "manager"):
            with pytest.raises(PermissionDenied):
                ns.save_rule(w["org_a"], actor=w[who], code="r1", data=data)
        with pytest.raises(PermissionDenied):
            ns.save_rule(org_b, actor=w["notifier"], code="r1", data=data)  # holds the permission in organization A only
        rule = ns.save_rule(w["org_a"], actor=w["notifier"], code="r1", data=data)
        with pytest.raises(PermissionDenied):
            ns.save_rule(org_b, actor=make_user(email="nt-b-admin@example.com", organization=org_b), rule=rule, data={"priority": "high"})

    def test_mandatory_rules_need_their_own_permission(self, w):
        data = {"name": "R", "event_type": "finding.raised", "channels": ["in_app"], "recipient_strategies": [{"type": "assignee"}], "preference_policy": "mandatory"}
        with pytest.raises(PermissionDenied):
            ns.save_rule(w["org_a"], actor=w["notifier"], code="r1", data=data)
        assert ns.save_rule(w["org_a"], actor=w["mandator"], code="r1", data=data).preference_policy == "mandatory"

    def test_the_workflow_task_strategy_notifies_eligible_task_holders_only(self, w, django_capture_on_commit_callbacks):
        from apps.workflows import services as wf

        from workflows_support import ca_workflow, submitted_action

        ca_workflow(w)
        with django_capture_on_commit_callbacks(execute=True):
            instance = wf.start(actor=None, target_type="corrective_action", target=submitted_action(w), definition_code="ca-verification", system=True)
        got = {n.recipient_id for n in Notification.objects.filter(event_type="workflow.task_assigned")}
        assert w["verifier"].pk in got and w["manager"].pk not in got and w["worker1"].pk not in got  # the verifier holds the policy permission; the owner is excluded by separation
        n = Notification.objects.get(recipient=w["verifier"], event_type="workflow.task_assigned")
        assert n.target_type == "workflow_task" and instance.reference and delivery(n, "email").status in {"pending", "sent"}  # (sent when the after-commit inline dispatch ran)


# ---------------------------------------------------------------------------------------------------------------- preferences
@pytest.mark.django_db
class TestPreferences:
    def notify(self, w, action, policy):
        ns.seed_defaults(w["org_a"])
        rule = default_rule(w)
        ns.save_rule(w["org_a"], actor=w["mandator"], rule=rule, data={"preference_policy": policy})
        fire(w, "corrective_action.assigned", action, w["manager"], disc=f"pref-{policy}")
        return Notification.objects.get(event__dedupe_key__endswith=f"pref-{policy}")

    def off(self, w, **kw):
        ns.set_preference(w["worker1"], w["org_a"], "corrective_actions", **kw)

    def test_no_row_means_everything_is_on(self, w, action):
        n = self.notify(w, action, "optional")
        assert n.in_app_visible and delivery(n, "email").status == "pending"

    def test_optional_notifications_can_be_switched_off_per_channel(self, w, action):
        self.off(w, in_app_enabled=False, email_enabled=False)
        n = self.notify(w, action, "optional")
        assert not n.in_app_visible and not selectors.inbox(w["worker1"]).exists()
        assert (delivery(n, "in_app").status, delivery(n, "in_app").suppressed_reason) == ("suppressed", "preference_in_app_off")
        assert (delivery(n, "email").status, delivery(n, "email").suppressed_reason) == ("suppressed", "preference_email_off")  # recorded, not silently dropped

    def test_operational_notifications_always_show_in_app_but_email_can_be_switched_off(self, w, action):
        self.off(w, in_app_enabled=False, email_enabled=False)
        n = self.notify(w, action, "operational")
        assert n.in_app_visible and delivery(n, "in_app").status == "delivered" and delivery(n, "email").status == "suppressed"

    def test_mandatory_notifications_cannot_be_switched_off(self, w, action):
        self.off(w, in_app_enabled=False, email_enabled=False)
        n = self.notify(w, action, "mandatory")
        assert n.in_app_visible and delivery(n, "in_app").status == "delivered" and delivery(n, "email").status == "pending"

    def test_one_category_does_not_affect_another(self, w, action):
        ns.set_preference(w["worker1"], w["org_a"], "findings", in_app_enabled=False, email_enabled=False)
        n = self.notify(w, action, "optional")
        assert n.in_app_visible

    def test_preferences_are_own_validated_and_audited(self, w):
        pref = ns.set_preference(w["worker1"], w["org_a"], "findings", email_enabled=False)
        assert pref.email_enabled is False and pref.in_app_enabled is True and ActivityLog.objects.filter(verb="notification.preference_changed").count() == 1
        ns.set_preference(w["worker1"], w["org_a"], "findings", email_enabled=False)  # no change: not audited again
        assert ActivityLog.objects.filter(verb="notification.preference_changed").count() == 1
        with pytest.raises(ns.NotificationError):
            ns.set_preference(w["worker1"], w["org_a"], "findings", email_enabled=False, actor=w["admin"])  # nobody edits someone else's preferences
        for bad in ({"category": "nope"}, {"category": "findings", "email_enabled": "no"}):
            with pytest.raises(ns.NotificationError):
                ns.set_preference(w["worker1"], w["org_a"], **bad)

    def test_a_non_member_cannot_hold_preferences_in_an_organization(self, w, org_b):
        with pytest.raises(ns.NotificationError) as info:
            ns.set_preference(w["worker1"], org_b, "findings", email_enabled=False)
        assert info.value.code == "not_member"


# ---------------------------------------------------------------------------------------------------------------- templates
@pytest.mark.django_db
class TestTemplates:
    FIELDS = {"subject": "Action: {{ title }}", "body_text": "Hi {{ recipient_name }}: {{ title }} ({{ reference }})", "body_html": "<p>{{ title }}</p>",
              "in_app_title": "Act on {{ reference }}", "in_app_message": "{{ title }}"}

    def make(self, w, code="ca-assigned", **over):
        draft = ns.save_template(w["org_a"], actor=w["notifier"], code=code, name="CA assigned", event_type="corrective_action.assigned", fields={**self.FIELDS, **over})
        return ns.activate_template(draft, actor=w["notifier"])

    def test_lifecycle_draft_active_archived_and_one_active_version(self, w):
        v1 = self.make(w)
        assert (v1.version, v1.status) == (1, "active")
        draft = ns.save_template(w["org_a"], actor=w["notifier"], code="ca-assigned", name="CA assigned", event_type="corrective_action.assigned", fields={**self.FIELDS, "subject": "New: {{ title }}"})
        assert (draft.version, draft.status) == (2, "draft")
        v2 = ns.activate_template(draft, actor=w["notifier"])
        v1.refresh_from_db()
        assert v2.status == "active" and v1.status == "archived" and NotificationTemplate.objects.filter(code="ca-assigned", status="active").count() == 1
        assert ActivityLog.objects.filter(verb="notification.template_activated").count() == 2
        with pytest.raises(ns.NotificationConflict):
            ns.activate_template(v2, actor=w["notifier"])

    def test_activation_validates_every_field(self, w):
        draft = ns.save_template(w["org_a"], actor=w["notifier"], code="bad", name="Bad", event_type="corrective_action.assigned", fields={**self.FIELDS, "body_text": "{% load os %}{{ secret }}"})
        with pytest.raises(ns.NotificationError) as info:
            ns.activate_template(draft, actor=w["notifier"])
        assert info.value.code == "invalid_template" and len(info.value.extra["problems"]) >= 1
        draft.refresh_from_db()
        assert draft.status == "draft"

    def test_template_administration_needs_permission(self, w):
        for who in ("viewer_only", "no_notif", "worker1"):
            with pytest.raises(PermissionDenied):
                ns.save_template(w["org_a"], actor=w[who], code="x", name="X", event_type="corrective_action.assigned", fields=self.FIELDS)
        draft = ns.save_template(w["org_a"], actor=w["notifier"], code="x", name="X", event_type="corrective_action.assigned", fields=self.FIELDS)
        with pytest.raises(PermissionDenied):
            ns.activate_template(draft, actor=w["viewer_only"])

    def test_an_active_template_is_used_by_a_rule_and_escapes_html(self, w):
        evil = make_action(w, title="<script>alert(1)</script>")
        self.make(w)
        ns.seed_defaults(w["org_a"])
        ns.save_rule(w["org_a"], actor=w["notifier"], rule=default_rule(w), data={"template_code": "ca-assigned"})
        fire(w, "corrective_action.assigned", evil, w["manager"])
        n = Notification.objects.get()
        d = delivery(n, "email")
        assert (n.template_code, n.template_version) == ("ca-assigned", 1) and n.title.startswith("Act on CA-") and "<script>" not in d.body_html
        assert "&lt;script&gt;" in d.body_html and d.subject == "Action: <script>alert(1)</script>"  # text stays text; only HTML is escaped
        assert d.body_text.startswith("Hi ")

    def test_a_missing_or_archived_template_falls_back_to_the_built_in_text(self, w, action):
        t = self.make(w)
        ns.seed_defaults(w["org_a"])
        ns.save_rule(w["org_a"], actor=w["notifier"], rule=default_rule(w), data={"template_code": "ca-assigned"})
        ns.archive_template(t, actor=w["notifier"])
        fire(w, "corrective_action.assigned", action, w["manager"])
        n = Notification.objects.get()
        assert n.template_code == "" and n.template_version == 0 and n.title == "Corrective action assigned"

    def test_a_template_for_another_event_cannot_be_attached(self, w):
        t = ns.save_template(w["org_a"], actor=w["notifier"], code="fnd", name="F", event_type="finding.raised", fields=self.FIELDS | {"body_text": "{{ title }}"})
        ns.activate_template(t, actor=w["notifier"])
        with pytest.raises(ns.NotificationError):
            ns.save_rule(w["org_a"], actor=w["notifier"], code="r1", data={"name": "R", "event_type": "corrective_action.assigned", "channels": ["in_app"],
                                                                         "recipient_strategies": [{"type": "assignee"}], "template_code": "fnd"})

    def test_preview_uses_synthetic_data_and_reports_problems(self, w):
        ok = ns.preview("corrective_action.assigned", self.FIELDS)
        assert ok["valid"] and ok["rendered"]["subject"] == "Action: Sample title" and "recipient_name" in ok["variables"]
        bad = ns.preview("corrective_action.assigned", {**self.FIELDS, "subject": "{{ secret }}"})
        assert not bad["valid"] and bad["rendered"] is None

    def test_another_organizations_templates_are_not_used(self, w, org_b):
        draft = ns.save_template(org_b, actor=w["b_admin"], code="ca-assigned", name="B", event_type="corrective_action.assigned", fields={**self.FIELDS, "in_app_title": "B ONLY"})
        ns.activate_template(draft, actor=w["b_admin"])
        assert config.active_template(org_b, "ca-assigned", "corrective_action.assigned") is not None
        assert config.active_template(w["org_a"], "ca-assigned", "corrective_action.assigned") is None  # organization A never sees organization B's template


# ---------------------------------------------------------------------------------------------------------------- in-app
@pytest.mark.django_db
class TestInApp:
    def three(self, w, action):
        for i in range(3):
            fire(w, "corrective_action.assigned", action, w["manager"], disc=f"n{i}")
        return list(selectors.inbox(w["worker1"]))

    def test_inbox_unread_count_and_mark_read(self, w, action):
        items = self.three(w, action)
        assert len(items) == 3 and selectors.unread_count(w["worker1"]) == 3
        read = ns.mark_read(w["worker1"], items[0].pk)
        assert read.is_read and read.read_at and selectors.unread_count(w["worker1"]) == 2
        assert ns.mark_read(w["worker1"], items[0].pk).read_at == read.read_at  # idempotent

    def test_bulk_mark_is_bounded_and_scoped_to_the_owner(self, w, action):
        items = self.three(w, action)
        assert ns.mark_many(w["worker1"], [str(i.pk) for i in items[:2]]) == 2 and selectors.unread_count(w["worker1"]) == 1
        assert ns.mark_many(w["manager"], [str(i.pk) for i in items]) == 0  # someone else's ids: nothing happens, nothing is confirmed
        with pytest.raises(ns.NotificationError):
            ns.mark_many(w["worker1"], [str(items[0].pk)] * (k.BULK_LIMIT + 1))
        assert ns.mark_all(w["worker1"]) == 1 and selectors.unread_count(w["worker1"]) == 0

    def test_other_peoples_notifications_do_not_exist_for_you(self, w, action):
        item = self.three(w, action)[0]
        assert selectors.get_notification(w["manager"], item.pk) is None and selectors.get_notification(w["b_admin"], item.pk) is None
        with pytest.raises(ns.NotificationError) as info:
            ns.mark_read(w["manager"], item.pk)
        assert info.value.code == "not_found"
        assert not selectors.inbox(w["manager"]).exists() and selectors.unread_count(w["b_admin"]) == 0

    def test_archive_hides_and_expiry_hides(self, w, action):
        items = self.three(w, action)
        ns.archive(w["worker1"], items[0].pk)
        assert selectors.inbox(w["worker1"]).count() == 2 and selectors.inbox(w["worker1"], include_archived=True).count() == 3
        Notification.objects.filter(pk=items[1].pk).update(expires_at=timezone.now() - timedelta(days=1))
        assert selectors.inbox(w["worker1"]).count() == 1

    def test_the_destination_is_computed_and_rechecked_for_the_viewer(self, w, action):
        item = self.three(w, action)[0]
        assert ns.inapp.destination(w["worker1"], item) == action.get_absolute_url()
        Notification.objects.filter(pk=item.pk).update()  # the notification itself never carries a URL
        assert ns.inapp.destination(w["no_notif"], item) == ""  # holding the notification is not permission to open the object
        assert ns.inapp.destination(w["b_admin"], item) == ""

    def test_filters_by_category_and_unread(self, w, action):
        self.three(w, action)
        assert selectors.inbox(w["worker1"], category="findings").count() == 0 and selectors.inbox(w["worker1"], category="corrective_actions", unread_only=True).count() == 3

    def test_the_inbox_query_is_constant_size(self, w, action, django_assert_max_num_queries):
        self.three(w, action)
        with django_assert_max_num_queries(3):
            list(selectors.inbox(w["worker1"])[:50])
            selectors.unread_count(w["worker1"])


# ---------------------------------------------------------------------------------------------------------------- email delivery
@pytest.mark.django_db
class TestEmailDelivery:
    def sent_email(self, w, action):
        fire(w, "corrective_action.assigned", action, w["manager"])
        return delivery(Notification.objects.get(), "email")

    def test_a_successful_send_is_provider_acceptance_not_confirmed_delivery(self, w, action, settings):
        d = self.sent_email(w, action)
        assert d.status == "pending" and not mail.outbox  # nothing is sent while the transaction that created it is still open
        assert ns.dispatch_batch() == {"claimed": 1, "sent": 1, "retry": 0, "failed": 0}
        d.refresh_from_db()
        message = mail.outbox[0]
        assert message.to == [w["worker1"].email] and message.subject.startswith(settings.EMAIL_SUBJECT_PREFIX) and "Action required: Repair the equipment" in message.subject
        assert "Repair the equipment" in message.body and d.to_masked == "c***@example.com"
        assert (d.status, d.attempt_count, d.provider) == ("sent", 1, "django") and d.accepted_at and d.delivered_at is None and d.lock_token == "" and d.next_attempt_at is None
        attempt = d.attempts.get()
        assert (attempt.number, attempt.outcome, attempt.error_category) == (1, "accepted", "")

    def test_html_and_plain_text_parts(self, w):
        t = ns.save_template(w["org_a"], actor=w["notifier"], code="html", name="H", event_type="corrective_action.assigned",
                             fields={"subject": "S {{ title }}", "body_text": "Plain {{ title }}", "body_html": "<b>{{ title }}</b>"})
        ns.activate_template(t, actor=w["notifier"])
        ns.seed_defaults(w["org_a"])
        ns.save_rule(w["org_a"], actor=w["notifier"], rule=default_rule(w), data={"template_code": "html"})
        fire(w, "corrective_action.assigned", make_action(w, title="A & B"), w["manager"])
        ns.dispatch_batch()
        message = mail.outbox[0]
        assert message.body == "Plain A & B" and message.alternatives[0].content == "<b>A &amp; B</b>" and message.alternatives[0].mimetype == "text/html"

    def test_a_recipient_without_a_usable_address_fails_permanently_and_visibly(self, w, action):
        w["worker1"].email = ""
        w["worker1"].save(update_fields=["email"])
        d = self.sent_email(w, action)
        ns.dispatch_batch()
        d.refresh_from_db()
        assert (d.status, d.last_error_category, d.attempt_count) == ("failed", "no_address", 1) and not mail.outbox and d.accepted_at is None
        assert d.attempts.get().outcome == "permanent_failure"

    def test_the_recipient_address_comes_from_the_user_never_from_the_request(self, w, action):
        d = self.sent_email(w, action)
        assert not hasattr(d, "to") and "@" not in d.to_masked.replace("***@", "")  # only a masked form is stored
        ns.dispatch_batch()
        assert mail.outbox[0].to == [w["worker1"].email]

    def test_dispatching_twice_never_sends_twice(self, w, action):
        self.sent_email(w, action)
        ns.dispatch_batch()
        assert ns.dispatch_batch()["claimed"] == 0 and len(mail.outbox) == 1

    def test_the_inline_dispatch_after_an_event_sends_once(self, w, action, settings):
        settings.NOTIFICATION_DISPATCH_INLINE = True
        event = ns.publish("corrective_action.assigned", action, actor=w["manager"], discriminator="inline", process=False)
        ns.process_event(event.pk, dispatch=True)
        ns.process_event(event.pk, dispatch=True)
        assert len(mail.outbox) == 1

    def test_the_email_channel_can_be_left_to_the_scheduler(self, w, action, settings):
        settings.NOTIFICATION_DISPATCH_INLINE = False
        event = ns.publish("corrective_action.assigned", action, actor=w["manager"], discriminator="deferred", process=False)
        ns.process_event(event.pk, dispatch=False)
        assert not mail.outbox and NotificationDelivery.objects.filter(channel="email", status="pending").count() == 1
        assert ns.process_event(event.pk, dispatch=True) == {"processed": False}  # processed events are not re-dispatched; the command does it
        assert ns.dispatch_batch()["sent"] == 1


@pytest.mark.django_db
class TestFailuresAndRetries:
    @pytest.fixture(autouse=True)
    def provider(self, settings):
        settings.NOTIFICATION_EMAIL_PROVIDER = SCRIPTED
        ScriptedProvider.reset()

    def queued(self, w, action):
        fire(w, "corrective_action.assigned", action, w["manager"])
        return delivery(Notification.objects.get(), "email")

    def test_a_transient_failure_schedules_a_bounded_retry_and_is_never_reported_as_sent(self, w, action):
        ScriptedProvider.reset(transient("timeout", None))
        d = self.queued(w, action)
        before = timezone.now()
        assert ns.dispatch_batch() == {"claimed": 1, "sent": 0, "retry": 1, "failed": 0}
        d.refresh_from_db()
        assert (d.status, d.attempt_count, d.last_error_category, d.accepted_at) == ("retry_pending", 1, "timeout", None) and d.next_attempt_at >= before + timedelta(seconds=59)
        assert "timed out" in d.last_error and d.lock_token == "" and not ScriptedProvider.sent
        assert ns.dispatch_batch()["claimed"] == 0  # not eligible yet
        assert not [x for x in selectors.dispatchable_deliveries()]

    def test_a_retry_that_succeeds_records_every_attempt(self, w, action):
        ScriptedProvider.reset(transient("connection", None), transient("provider_error", 503))
        d = self.queued(w, action)
        later = timezone.now() + timedelta(days=1)
        ns.dispatch_batch()
        ns.dispatch_batch(now=later)
        ns.dispatch_batch(now=later + timedelta(days=1))
        d.refresh_from_db()
        assert (d.status, d.attempt_count) == ("sent", 3) and [a.outcome for a in d.attempts.all()] == ["transient_failure", "transient_failure", "accepted"]
        assert d.attempts.last().provider_message_id == "msg-1" and len(ScriptedProvider.sent) == 1 and d.last_error_category == ""
        assert Notification.objects.count() == 1  # retries never duplicate the in-app notification

    def test_exhausting_the_attempts_fails_the_delivery_and_raises_an_operational_notice(self, w, action, settings):
        ScriptedProvider.reset(*[transient("provider_error", 503) for _ in range(5)])
        d = self.queued(w, action)
        NotificationDelivery.objects.filter(pk=d.pk).update(max_attempts=2)
        ns.dispatch_batch()
        ns.dispatch_batch(now=timezone.now() + timedelta(days=1))
        ns.process_pending(10, dispatch=False)  # the operational notice is an outbox event like any other
        d.refresh_from_db()
        assert (d.status, d.attempt_count, d.next_attempt_at) == ("failed", 2, None) and ns.dispatch_batch(now=timezone.now() + timedelta(days=9))["claimed"] == 0
        assert ActivityLog.objects.filter(verb="notification.delivery_exhausted").count() == 1
        notices = list(Notification.objects.filter(event_type="system.delivery_failed"))
        assert notices and {x.recipient_id for x in notices} <= {w["notifier"].pk, w["mandator"].pk, w["admin"].pk}  # every holder of notification.retry is told, once each
        assert len({x.recipient_id for x in notices}) == len(notices) and all("provider error" in x.message and in_app_only(x) for x in notices)

    def test_a_permanent_failure_is_not_retried(self, w, action):
        ScriptedProvider.reset(permanent("invalid_recipient", 400))
        d = self.queued(w, action)
        assert ns.dispatch_batch()["failed"] == 1
        d.refresh_from_db()
        assert (d.status, d.attempt_count, d.last_error_category) == ("failed", 1, "invalid_recipient") and d.attempts.get().outcome == "permanent_failure"
        assert ns.dispatch_batch(now=timezone.now() + timedelta(days=9))["claimed"] == 0

    def test_an_unexpected_provider_bug_is_a_sanitized_transient_failure(self, w, action):
        ScriptedProvider.reset(RuntimeError("password=hunter2 in a traceback"))
        d = self.queued(w, action)
        ns.dispatch_batch()
        d.refresh_from_db()
        assert (d.status, d.last_error_category) == ("retry_pending", "unexpected") and "hunter2" not in d.last_error and "hunter2" not in str(list(ActivityLog.objects.values_list("metadata", flat=True)))

    def test_one_failing_delivery_does_not_stop_the_batch(self, w, action):
        second = make_action(w, title="Second action")
        fire(w, "corrective_action.assigned", action, w["manager"], disc="a")
        fire(w, "corrective_action.assigned", second, w["manager"], disc="b")
        ScriptedProvider.reset(permanent("rejected", 400))
        assert ns.dispatch_batch() == {"claimed": 2, "sent": 1, "retry": 0, "failed": 1}
        assert sorted(NotificationDelivery.objects.filter(channel="email").values_list("status", flat=True)) == ["failed", "sent"]

    def test_provider_unavailability_never_touches_the_business_data(self, w, action):
        ScriptedProvider.reset(transient("connection", None))
        action.refresh_from_db()
        status_before = action.status
        d = self.queued(w, action)
        ns.dispatch_batch()
        action.refresh_from_db()
        d.refresh_from_db()
        assert action.status == status_before and d.status == "retry_pending"

    def test_a_stuck_processing_delivery_is_reclaimed_after_the_lock_timeout(self, w, action):
        d = self.queued(w, action)
        claimed = ns_claim(1)
        assert claimed and ns_claim(1) == []  # locked: nobody else gets it
        NotificationDelivery.objects.filter(pk=d.pk).update(locked_at=timezone.now() - timedelta(seconds=k.LOCK_TIMEOUT_SECONDS + 5))
        assert len(ns_claim(1)) == 1  # the worker crashed: it is claimed again

    def test_a_stale_worker_cannot_overwrite_a_newer_claim(self, w, action):
        d = self.queued(w, action)
        (pk, token), = ns_claim(1)
        NotificationDelivery.objects.filter(pk=d.pk).update(locked_at=timezone.now() - timedelta(seconds=k.LOCK_TIMEOUT_SECONDS + 5))
        ns_claim(1)  # a second worker takes over with a new token
        assert ns.delivery.send_claimed(pk, token) == "processing" and not ScriptedProvider.sent  # the first worker's token no longer matches: it does nothing


def in_app_only(notice) -> bool:
    return list(notice.deliveries.values_list("channel", flat=True)) == ["in_app"]


def ns_claim(limit):
    return ns.delivery.claim(limit)


# ---------------------------------------------------------------------------------------------------------------- manual operations
@pytest.mark.django_db
class TestManualOperations:
    @pytest.fixture(autouse=True)
    def provider(self, settings):
        settings.NOTIFICATION_EMAIL_PROVIDER = SCRIPTED
        ScriptedProvider.reset()

    def failed(self, w, action):
        ScriptedProvider.reset(permanent("rejected", 400))
        fire(w, "corrective_action.assigned", action, w["manager"])
        d = delivery(Notification.objects.get(), "email")
        ns.dispatch_batch()
        d.refresh_from_db()
        assert d.status == "failed"
        return d

    def test_a_manual_retry_needs_permission_and_a_reason_and_gives_a_fresh_bounded_allowance(self, w, action):
        d = self.failed(w, action)
        for who in ("viewer_only", "no_notif", "worker1", "manager"):
            with pytest.raises(PermissionDenied):
                ns.retry(d, actor=w[who], reason="x")
        with pytest.raises(ns.NotificationError) as info:
            ns.retry(d, actor=w["notifier"], reason="  ")
        assert info.value.code == "reason_required"
        retried = ns.retry(d, actor=w["notifier"], reason="Provider fixed")
        assert (retried.status, retried.attempt_count, retried.max_attempts) == ("pending", 1, 1 + k.MAX_ATTEMPTS_DEFAULT)
        assert ActivityLog.objects.filter(verb="notification.manual_retry", metadata__reason="Provider fixed").count() == 1
        ns.dispatch_batch()
        retried.refresh_from_db()
        assert retried.status == "sent" and retried.attempt_count == 2 and len(ScriptedProvider.sent) == 1 and Notification.objects.count() == 1

    def test_only_a_failed_email_can_be_retried(self, w, action):
        fire(w, "corrective_action.assigned", action, w["manager"])
        d = delivery(Notification.objects.get(), "email")
        with pytest.raises(ns.NotificationConflict):
            ns.retry(d, actor=w["notifier"], reason="x")
        with pytest.raises(ns.NotificationConflict):
            ns.retry(delivery(Notification.objects.get(), "in_app"), actor=w["notifier"], reason="x")

    def test_a_deliberate_resend_is_a_new_delivery_with_its_own_key_and_reason(self, w, action):
        fire(w, "corrective_action.assigned", action, w["manager"])
        original = delivery(Notification.objects.get(), "email")
        ns.dispatch_batch()
        new = ns.resend(original, actor=w["notifier"], reason="The recipient lost it")
        assert new.pk != original.pk and new.resend_of_id == original.pk and new.resend_reason == "The recipient lost it" and new.status == "pending"
        assert new.idempotency_key == f"{original.notification_id}|email|1" and new.subject == original.subject
        ns.dispatch_batch()
        assert len(ScriptedProvider.sent) == 2 and NotificationDelivery.objects.filter(channel="email").count() == 2 and Notification.objects.count() == 1
        again = ns.resend(new, actor=w["notifier"], reason="Once more")
        assert again.resend_of_id == original.pk and again.idempotency_key.endswith("|email|2")  # chains point at the original

    def test_resend_needs_permission_reason_and_a_resendable_state(self, w, action):
        fire(w, "corrective_action.assigned", action, w["manager"])
        d = delivery(Notification.objects.get(), "email")
        with pytest.raises(ns.NotificationConflict):
            ns.resend(d, actor=w["notifier"], reason="x")  # still pending
        ns.dispatch_batch()
        with pytest.raises(PermissionDenied):
            ns.resend(d, actor=w["viewer_only"], reason="x")
        with pytest.raises(ns.NotificationError):
            ns.resend(d, actor=w["notifier"], reason="")

    def test_cancel(self, w, action):
        fire(w, "corrective_action.assigned", action, w["manager"])
        d = delivery(Notification.objects.get(), "email")
        with pytest.raises(PermissionDenied):
            ns.cancel(d, actor=w["worker1"], reason="x")
        assert ns.cancel(d, actor=w["notifier"], reason="No longer needed").status == "cancelled"
        assert ns.dispatch_batch()["claimed"] == 0 and not ScriptedProvider.sent
        with pytest.raises(ns.NotificationConflict):
            ns.cancel(d, actor=w["notifier"], reason="again")

    def test_only_a_provider_can_confirm_delivery_and_only_for_sent_messages(self, w, action):
        fire(w, "corrective_action.assigned", action, w["manager"])
        d = delivery(Notification.objects.get(), "email")
        with pytest.raises(ns.NotificationConflict):
            ns.confirm_delivery(d)  # not even sent
        ns.dispatch_batch()
        confirmed = ns.confirm_delivery(d, provider_message_id="abc")
        assert confirmed.status == "delivered" and confirmed.delivered_at and confirmed.provider_message_id == "abc" and ns.confirm_delivery(d).status == "delivered"


# ---------------------------------------------------------------------------------------------------------------- concurrency, guards, retention, reminders
@pytest.mark.django_db(transaction=True)
class TestConcurrency:
    def test_two_dispatchers_never_send_the_same_delivery(self, w, action, settings):
        settings.NOTIFICATION_EMAIL_PROVIDER = SCRIPTED
        settings.NOTIFICATION_DISPATCH_INLINE = False  # (a transactional test commits for real, so the domain events fire too; dispatch only from the threads)
        for i in range(6):
            fire(w, "corrective_action.assigned", make_action(w, title=f"Action {i}"), w["manager"], disc=f"c{i}")
        ScriptedProvider.reset()
        total = NotificationDelivery.objects.filter(channel="email", status="pending").count()
        assert total >= 6
        results = []

        def work():
            try:
                results.append(ns.dispatch_batch(limit=total))
            finally:
                connection.close()

        threads = [threading.Thread(target=work) for _ in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(90)
        assert sum(r["sent"] for r in results) == total and len(ScriptedProvider.sent) == total
        assert set(NotificationDelivery.objects.filter(channel="email").values_list("attempt_count", flat=True)) == {1}
        assert set(NotificationDelivery.objects.filter(channel="email").values_list("status", flat=True)) == {"sent"}

    def test_concurrent_publication_of_the_same_event_creates_one_notification(self, w, action):
        results = []

        def work():
            try:
                event = ns.publish("corrective_action.assigned", action, actor=w["manager"], discriminator="race", process=False)
                results.append(ns.process_event(event.pk, dispatch=False))
            finally:
                connection.close()

        threads = [threading.Thread(target=work) for _ in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(90)
        mine = NotificationEvent.objects.filter(dedupe_key__endswith=":race")
        assert mine.count() == 1 and Notification.objects.filter(event__in=mine).count() == 1 and sum(1 for r in results if r.get("processed")) == 1


@pytest.mark.django_db
class TestDatabaseGuards:
    def make(self, w, action):
        fire(w, "corrective_action.assigned", action, w["manager"])
        return Notification.objects.get()

    def test_a_notification_cannot_be_rewritten_or_deleted(self, w, action):
        n = self.make(w, action)
        for change in ({"title": "x"}, {"message": "x"}, {"target_id": w["org_a"].pk}, {"recipient": w["manager"]}, {"category": "findings"}):
            with pytest.raises(IntegrityError), transaction.atomic():
                Notification.objects.filter(pk=n.pk).update(**change)
        with pytest.raises(IntegrityError), transaction.atomic():
            Notification.objects.filter(pk=n.pk).delete()
        Notification.objects.filter(pk=n.pk).update(is_read=True, read_at=timezone.now(), archived_at=timezone.now())  # reading and archiving are allowed

    def test_the_delivery_state_machine_is_enforced_by_the_database(self, w, action):
        n = self.make(w, action)
        d = delivery(n, "email")
        for target in ("sent", "delivered", "failed"):  # pending cannot jump anywhere but processing / cancelled
            with pytest.raises(IntegrityError), transaction.atomic():
                NotificationDelivery.objects.filter(pk=d.pk).update(status=target, accepted_at=timezone.now(), delivered_at=timezone.now())
        NotificationDelivery.objects.filter(pk=d.pk).update(status="processing")
        NotificationDelivery.objects.filter(pk=d.pk).update(status="sent", accepted_at=timezone.now())
        with pytest.raises(IntegrityError), transaction.atomic():
            NotificationDelivery.objects.filter(pk=d.pk).update(status="pending")  # a sent message is never "unsent"
        NotificationDelivery.objects.filter(pk=d.pk).update(status="delivered", delivered_at=timezone.now())
        with pytest.raises(IntegrityError), transaction.atomic():
            NotificationDelivery.objects.filter(pk=d.pk).update(status="failed")

    def test_delivery_identity_and_deletion(self, w, action):
        n = self.make(w, action)
        d = delivery(n, "email")
        for change in ({"channel": "in_app"}, {"idempotency_key": "other"}):
            with pytest.raises(IntegrityError), transaction.atomic():
                NotificationDelivery.objects.filter(pk=d.pk).update(**change)
        with pytest.raises(IntegrityError), transaction.atomic():
            NotificationDelivery.objects.filter(pk=d.pk).delete()

    def test_constraints_reject_false_success_states(self, w, action):
        d = delivery(self.make(w, action), "email")
        with pytest.raises(IntegrityError), transaction.atomic():
            NotificationDelivery.objects.filter(pk=d.pk).update(status="processing")
            NotificationDelivery.objects.filter(pk=d.pk).update(status="sent")  # sent without a provider acceptance time
        with pytest.raises(IntegrityError), transaction.atomic():
            NotificationDelivery.objects.filter(pk=d.pk).update(status="processing")
            NotificationDelivery.objects.filter(pk=d.pk).update(status="retry_pending")  # a retry without a time is not a retry
        with pytest.raises(IntegrityError), transaction.atomic():
            Notification.objects.filter(pk=d.notification_id).update(is_read=True)  # read without a read time

    def test_attempts_events_and_keys(self, w, action):
        n = self.make(w, action)
        d = delivery(n, "email")
        attempt = DeliveryAttempt.objects.create(delivery=d, number=1, started_at=timezone.now(), finished_at=timezone.now(), outcome="accepted")
        for op in (lambda: DeliveryAttempt.objects.filter(pk=attempt.pk).update(outcome="transient_failure"), lambda: DeliveryAttempt.objects.filter(pk=attempt.pk).delete()):
            with pytest.raises(IntegrityError), transaction.atomic():
                op()
        with pytest.raises(IntegrityError), transaction.atomic():
            DeliveryAttempt.objects.create(delivery=d, number=1, started_at=timezone.now(), finished_at=timezone.now(), outcome="accepted")
        event = n.event
        with pytest.raises(IntegrityError), transaction.atomic():
            NotificationEvent.objects.filter(pk=event.pk).update(source_id=uuid.UUID(int=1))
        with pytest.raises(IntegrityError), transaction.atomic():
            NotificationEvent.objects.filter(pk=event.pk).delete()
        with pytest.raises(IntegrityError), transaction.atomic():
            Notification.objects.create(organization=n.organization, recipient=n.recipient, event=event, rule_code="x", event_type=n.event_type, category="findings", title="t", target_type="finding",
                                        target_id=n.target_id, idempotency_key=n.idempotency_key)  # the same event never notifies the same person twice


@pytest.mark.django_db
class TestRetentionAndReminders:
    def test_retention_removes_old_closed_notifications_only(self, w, action):
        fire(w, "corrective_action.assigned", action, w["manager"], disc="old")
        old = Notification.objects.get()
        d = delivery(old, "email")
        ns.cancel(d, actor=w["notifier"], reason="close it")
        fire(w, "corrective_action.assigned", action, w["manager"], disc="fresh")
        pending = Notification.objects.exclude(pk=old.pk).get()
        with connection.cursor() as cursor:  # age both (the test needs an old row; the guard allows time columns to move only through the purge setting for deletes)
            cursor.execute("UPDATE notifications_notification SET created_at = created_at - interval '400 days'")
        result = ns.purge(days=180)
        assert result["notifications"] == 1 and not Notification.objects.filter(pk=old.pk).exists() and Notification.objects.filter(pk=pending.pk).exists()  # the pending email is kept
        assert ActivityLog.objects.filter(verb="notification.retention_purge").count() == 1 and action.__class__.objects.filter(pk=action.pk).exists()

    def test_due_reminders_are_idempotent_per_due_date(self, w):
        soon = make_action(w, due_at=timezone.now() + timedelta(hours=10))
        late = make_action(w, title="Overdue one")
        type(late).objects.filter(pk=late.pk).update(due_at=timezone.now() - timedelta(hours=2))
        late.refresh_from_db()
        make_action(w, title="Far away", due_at=timezone.now() + timedelta(days=30))
        assert ns.emit_due_reminders(within_hours=48) == {"due_soon": 1, "overdue": 1}
        assert ns.emit_due_reminders(within_hours=48) == {"due_soon": 0, "overdue": 0}  # same state: nothing new
        ns.process_pending(10, dispatch=False)
        got = {(n.event_type, n.target_id) for n in Notification.objects.all()}
        assert ("corrective_action.due_soon", soon.pk) in got and ("corrective_action.overdue", late.pk) in got and Notification.objects.count() == 2
        soon.due_at = soon.due_at + timedelta(hours=5)
        soon.save(update_fields=["due_at"])
        assert ns.emit_due_reminders(within_hours=48)["due_soon"] == 1  # an extended due date earns a fresh reminder
