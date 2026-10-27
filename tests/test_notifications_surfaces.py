"""Phase 17 surfaces: the Graph provider, the web screens, the REST API, the admin, the management commands and the domain-event integration."""

from __future__ import annotations

import io
import urllib.error
from datetime import timedelta

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import Client, override_settings
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

from apps.notifications import services as ns
from apps.notifications.models import Notification, NotificationDelivery, NotificationEvent, NotificationRule
from apps.notifications.providers.base import EmailMessage, EmailProviderError
from apps.notifications.providers.microsoft365 import Microsoft365EmailProvider

from actions_support import make_action, to_status
from notification_support import build_notification_world

API = "/api/v1/notifications/"


@pytest.fixture
def w(org_a, org_b, make_user, assign, make_type):
    return build_notification_world(org_a, org_b, make_user, assign, make_type)


@pytest.fixture
def action(w):
    return make_action(w)


def web(user):
    client = Client()
    client.force_login(user)
    return client


def api(user=None):
    client = APIClient()
    if user is not None:
        client.force_authenticate(user)
    return client


def body(response):
    data = response.json()
    return data.get("data", data) if isinstance(data, dict) else data


def notify(w, action, disc="s1"):
    event = ns.publish("corrective_action.assigned", action, actor=w["manager"], discriminator=disc, process=False)
    ns.process_event(event.pk, dispatch=False)
    return Notification.objects.filter(event=event).first()


# --------------------------------------------------------------------------------------------------------------------- Graph provider
class TestGraphProvider:
    MESSAGE = EmailMessage(subject="Hello", body="Plain", to=["a@example.com"])

    def provider(self, monkeypatch, outcome):
        provider = Microsoft365EmailProvider()
        monkeypatch.setattr(provider, "_token", lambda: "token")
        calls = []

        def post(url, token, payload):
            calls.append((url, payload))
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

        monkeypatch.setattr(provider, "_post", post)
        return provider, calls

    @override_settings(NOTIFICATION_GRAPH_SENDER="sender@example.com")
    def test_a_202_is_acceptance_not_confirmed_delivery(self, monkeypatch):
        provider, calls = self.provider(monkeypatch, 202)
        result = provider.send(self.MESSAGE)
        assert result.status_code == 202 and result.confirmed is False
        assert calls[0][0].endswith("/users/sender@example.com/sendMail") and calls[0][1]["message"]["toRecipients"][0]["emailAddress"]["address"] == "a@example.com"

    @pytest.mark.parametrize("status,category,transient", [(401, "unauthorized", False), (403, "unauthorized", False), (429, "rate_limited", True), (503, "provider_error", True), (400, "rejected", False)])
    @override_settings(NOTIFICATION_GRAPH_SENDER="sender@example.com")
    def test_http_failures_are_classified(self, monkeypatch, status, category, transient):
        provider, _ = self.provider(monkeypatch, urllib.error.HTTPError("https://graph", status, "x", {}, None))
        with pytest.raises(EmailProviderError) as info:
            provider.send(self.MESSAGE)
        assert (info.value.category, info.value.transient) == (category, transient)

    @override_settings(NOTIFICATION_GRAPH_SENDER="sender@example.com")
    def test_network_failures_are_transient_and_leak_nothing(self, monkeypatch):
        provider, _ = self.provider(monkeypatch, urllib.error.URLError("secret-host-detail"))
        with pytest.raises(EmailProviderError) as info:
            provider.send(self.MESSAGE)
        assert info.value.transient and "secret-host-detail" not in str(info.value)

    @override_settings(MICROSOFT_AUTH_CONFIGURED=False, NOTIFICATION_GRAPH_SENDER="")
    def test_an_unconfigured_provider_never_pretends_to_send(self):
        with pytest.raises(EmailProviderError) as info:
            Microsoft365EmailProvider().send(self.MESSAGE)
        assert info.value.category == "not_configured" and not info.value.transient


# ---------------------------------------------------------------------------------------------------------------------------------- web
@pytest.mark.django_db
class TestWeb:
    def test_anonymous_visitors_are_sent_to_sign_in(self):
        for name in ("notifications:inbox", "notifications:preferences", "notifications:rules", "notifications:deliveries"):
            assert Client().get(reverse(name)).status_code == 302

    def test_the_inbox_lists_only_my_notifications_and_the_bell_shows_the_count(self, w, action):
        n = notify(w, action)
        page = web(n.recipient).get(reverse("notifications:inbox"))
        assert page.status_code == 200 and n.title in page.content.decode()
        other = web(w["no_notif"]).get(reverse("notifications:inbox")).content.decode()
        assert n.title not in other

    def test_opening_marks_read_and_redirects_only_when_allowed(self, w, action):
        n = notify(w, action)
        r = web(n.recipient).get(reverse("notifications:open", args=[n.pk]))
        n.refresh_from_db()
        assert r.status_code == 302 and n.is_read and r.url.startswith("/")
        assert web(w["no_notif"]).get(reverse("notifications:open", args=[n.pk])).status_code == 404  # never someone else's

    def test_posting_read_and_archive_needs_ownership(self, w, action):
        n = notify(w, action)
        assert web(w["no_notif"]).post(reverse("notifications:read", args=[n.pk])).status_code == 404
        assert web(n.recipient).post(reverse("notifications:archive", args=[n.pk])).status_code == 302
        n.refresh_from_db()
        assert n.archived_at is not None

    def test_preferences_page_saves(self, w):
        client = web(w["worker1"])
        assert client.get(reverse("notifications:preferences")).status_code == 200
        r = client.post(reverse("notifications:preferences"), {"in_app_assignments_present": "1", "email_assignments_present": "1"})
        assert r.status_code == 302

    def test_admin_screens_need_the_notification_permissions(self, w):
        assert web(w["no_notif"]).get(reverse("notifications:rules")).status_code == 403
        assert web(w["notifier"]).get(reverse("notifications:rules")).status_code == 200
        assert web(w["notifier"]).get(reverse("notifications:deliveries")).status_code == 200
        assert web(w["viewer_only"]).get(reverse("notifications:deliveries")).status_code == 403

    def test_rule_toggle_requires_configure(self, w):
        ns.seed_defaults(w["org_a"])
        rule = NotificationRule.objects.filter(organization=w["org_a"]).first()
        assert web(w["viewer_only"]).post(reverse("notifications:rule_toggle", args=[rule.pk]), {"action": "disable"}).status_code == 403
        assert web(w["notifier"]).post(reverse("notifications:rule_toggle", args=[rule.pk]), {"action": "disable"}).status_code == 302
        rule.refresh_from_db()
        assert rule.enabled is False

    def test_delivery_action_requires_retry_permission_and_a_reason(self, w, action):
        n = notify(w, action)
        d = NotificationDelivery.objects.filter(notification=n, channel="email").first()
        assert d is not None
        url = reverse("notifications:delivery_action", args=[d.pk, "cancel"])
        assert web(w["no_notif"]).post(url, {"reason": "x"}).status_code == 403
        web(w["notifier"]).post(url, {"reason": ""})
        d.refresh_from_db()
        assert d.status == "pending"  # no reason: nothing happened
        web(w["notifier"]).post(url, {"reason": "not needed"})
        d.refresh_from_db()
        assert d.status == "cancelled"


# ---------------------------------------------------------------------------------------------------------------------------------- API
@pytest.mark.django_db
class TestApi:
    def test_authentication_is_required(self):
        assert api().get(API).status_code in (401, 403)

    def test_list_detail_unread_count_and_mark_read(self, w, action):
        n = notify(w, action)
        client = api(n.recipient)
        listing = client.get(API).json()  # the standard paginated envelope: {"data": [...], "meta": {"count": ...}}
        assert listing["meta"]["count"] == 1 and listing["data"][0]["id"] == str(n.pk)
        assert body(client.get(API + "unread-count/"))["unread"] == 1
        assert client.get(f"{API}{n.pk}/").status_code == 200
        assert body(client.post(f"{API}{n.pk}/read/"))["is_read"] is True
        assert body(client.get(API + "unread-count/"))["unread"] == 0

    def test_other_users_notifications_are_404(self, w, action):
        n = notify(w, action)
        other = api(w["no_notif"])
        assert other.get(f"{API}{n.pk}/").status_code == 404 and other.post(f"{API}{n.pk}/read/").status_code == 404 and other.post(f"{API}{n.pk}/archive/").status_code == 404

    def test_bulk_read_is_bounded_and_strict(self, w, action):
        n = notify(w, action)
        client = api(n.recipient)
        assert client.post(API + "mark-read/", {"ids": ["00000000-0000-0000-0000-000000000000"] * 201}, format="json").status_code == 400
        assert client.post(API + "mark-read/", {"bogus": 1}, format="json").status_code == 400
        assert client.post(API + "mark-read/", {}, format="json").status_code == 400
        assert body(client.post(API + "mark-read/", {"all": True}, format="json"))["marked"] == 1

    def test_no_endpoint_creates_notifications_or_deliveries(self, w, action):
        client = api(w["notifier"])
        for url in (API, API + "deliveries/"):
            assert client.post(url, {"title": "x"}, format="json").status_code in (400, 403, 405)
        n = notify(w, action)
        d = NotificationDelivery.objects.filter(notification=n, channel="email").first()
        assert client.put(f"{API}deliveries/{d.pk}/", {"status": "delivered"}, format="json").status_code == 405

    def test_preferences_roundtrip_and_validation(self, w):
        client = api(w["worker1"])
        got = body(client.put(API + "preferences/", {"preferences": [{"category": "assignments", "email_enabled": False}]}, format="json"))
        assert any(p["category"] == "assignments" and p["email_enabled"] is False for p in got["preferences"])  # (a plain object: not a list, so it is not paginated)
        assert client.put(API + "preferences/", {"preferences": [{"category": "nope"}]}, format="json").status_code == 400

    def test_rules_need_permissions_and_validate(self, w):
        assert api(w["no_notif"]).get(API + "rules/").json()["meta"]["count"] == 0  # nothing visible without notification.view
        ns.seed_defaults(w["org_a"])
        rules = api(w["notifier"]).get(API + "rules/").json()
        assert rules["meta"]["count"] > 0
        rule_id = rules["data"][0]["id"]
        assert api(w["viewer_only"]).post(f"{API}rules/{rule_id}/disable/").status_code == 403
        assert body(api(w["notifier"]).post(f"{API}rules/{rule_id}/disable/"))["enabled"] is False
        bad = api(w["notifier"]).post(API + "rules/", {"code": "x", "name": "X", "event_type": "nope.nothing", "channels": ["in_app"]}, format="json")
        assert bad.status_code == 400

    def test_mandatory_rules_need_the_separate_permission(self, w):
        payload = {"code": "m1", "name": "M", "event_type": "corrective_action.assigned", "channels": ["in_app"], "recipient_strategies": [{"type": "assignee"}], "preference_policy": "mandatory"}
        assert api(w["notifier"]).post(API + "rules/", payload, format="json").status_code == 403
        assert api(w["mandator"]).post(API + "rules/", payload, format="json").status_code == 201

    def test_template_preview_rejects_unsafe_templates(self, w):
        client = api(w["notifier"])
        ok = client.post(API + "templates/preview/", {"event_type": "corrective_action.assigned", "subject": "Hi {{ recipient_name }}"}, format="json")
        assert ok.status_code == 200
        for evil in ("{% load os %}", "{{ recipient_name.__class__ }}", "{{ recipient_name|upper }}"):
            result = client.post(API + "templates/preview/", {"event_type": "corrective_action.assigned", "subject": evil}, format="json")
            assert result.status_code == 200 and body(result)["valid"] is False and body(result)["rendered"] is None

    def test_deliveries_visibility_and_manual_operations(self, w, action):
        n = notify(w, action)
        d = NotificationDelivery.objects.get(notification=n, channel="email")
        assert api(w["no_notif"]).get(f"{API}deliveries/{d.pk}/").status_code in (403, 404)
        assert body(api(w["notifier"]).get(f"{API}deliveries/{d.pk}/"))["attempts"] == []
        assert api(w["notifier"]).post(f"{API}deliveries/{d.pk}/cancel/", {}, format="json").status_code == 400  # a reason is mandatory
        assert body(api(w["notifier"]).post(f"{API}deliveries/{d.pk}/cancel/", {"reason": "duplicate"}, format="json"))["status"] == "cancelled"
        assert api(w["notifier"]).post(f"{API}deliveries/{d.pk}/explode/", {"reason": "x"}, format="json").status_code == 404

    def test_metadata_lists_events_and_never_secrets(self, w):
        data = body(api(w["worker1"]).get(API + "metadata/"))
        assert any(e["code"] == "corrective_action.assigned" for e in data["events"])
        assert "secret" not in str(data).lower()

    def test_api_root_lists_the_notification_endpoints(self, w):
        root = body(api(w["worker1"]).get("/api/v1/"))
        assert "notifications" in str(root)


# -------------------------------------------------------------------------------------------------------------------------------- admin
@pytest.mark.django_db
class TestAdmin:
    def test_every_model_is_registered_read_only(self, admin_client):
        for model in ("notificationrule", "notificationtemplate", "notificationevent", "notification", "notificationdelivery", "deliveryattempt", "usernotificationpreference"):
            assert admin_client.get(f"/admin/notifications/{model}/").status_code == 200
            assert admin_client.get(f"/admin/notifications/{model}/add/").status_code == 403


# ------------------------------------------------------------------------------------------------------------------------------ commands
@pytest.mark.django_db
class TestCommands:
    def run(self, *args):
        out = io.StringIO()
        call_command(*args, stdout=out)
        return out.getvalue()

    def test_process_and_dispatch_commands_are_bounded_and_idempotent(self, w, action):
        ns.publish("corrective_action.assigned", action, actor=w["manager"], discriminator="c1", process=False)
        assert "Processed" in self.run("process_notification_events", "--limit", "10", "--no-dispatch")
        assert Notification.objects.count() > 0
        before = Notification.objects.count()
        self.run("process_notification_events")
        assert Notification.objects.count() == before
        assert "Dispatched" in self.run("dispatch_notifications", "--limit", "10")
        for bad in (["process_notification_events", "--limit", "0"], ["dispatch_notifications", "--limit", "5000"]):
            with pytest.raises(CommandError):
                self.run(*bad)

    def test_seed_defaults_is_idempotent(self, w):
        assert "Created" in self.run("seed_notification_defaults")
        count = NotificationRule.objects.count()
        self.run("seed_notification_defaults")
        assert NotificationRule.objects.count() == count

    def test_reminders_status_and_purge(self, w, action):
        assert "New reminder events" in self.run("emit_due_reminders")
        assert "Events:" in self.run("notification_status")
        assert "Purged" in self.run("purge_notifications", "--days", "365")
        with pytest.raises(CommandError):
            self.run("emit_due_reminders", "--within-hours", "0")


# ----------------------------------------------------------------------------------------------------------------- domain integration
@pytest.mark.django_db
class TestIntegration:
    def test_a_business_commit_publishes_an_event_that_becomes_a_notification(self, w, django_capture_on_commit_callbacks):
        action = make_action(w)
        with django_capture_on_commit_callbacks(execute=True):
            to_status(w, action, "submitted")
        assert NotificationEvent.objects.filter(event_type="corrective_action.submitted", source_id=action.pk).exists()
        assert Notification.objects.filter(event_type="corrective_action.submitted", target_id=action.pk).exists()

    def test_a_rolled_back_business_change_publishes_nothing(self, w, django_capture_on_commit_callbacks):
        from django.db import transaction

        action = make_action(w)
        before = NotificationEvent.objects.count()
        with django_capture_on_commit_callbacks(execute=True):
            try:
                with transaction.atomic():
                    to_status(w, action, "submitted")
                    raise RuntimeError("boom")
            except RuntimeError:
                pass
        assert NotificationEvent.objects.count() == before

    @override_settings(NOTIFICATION_DISPATCH_INLINE=False)
    def test_with_inline_dispatch_off_deliveries_wait_for_the_command(self, w, django_capture_on_commit_callbacks):
        action = make_action(w)
        with django_capture_on_commit_callbacks(execute=True):
            to_status(w, action, "submitted")
        deliveries = NotificationDelivery.objects.filter(channel="email")
        assert deliveries.exists() and not deliveries.exclude(status="pending").exists()
        assert ns.dispatch_batch()["claimed"] >= 1
        assert timezone.now() - timedelta(minutes=5) < timezone.now()
