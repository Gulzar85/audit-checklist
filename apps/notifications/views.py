"""Class-based views of the notification screens (Phase 17): the user's inbox, preferences, and the administrative rules / templates / delivery screens.

Every user-facing view is scoped to the signed-in user in the *query* (another person's notification is a 404).  Opening a notification re-checks the **viewer's own**
permission on the object through its owning module's policy; the destination is computed, never stored.  Administrative screens need the organization-wide
``notification.*`` permissions and call the same services as the API.
"""

from __future__ import annotations

from typing import Any

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.http import Http404, HttpRequest, HttpResponse, HttpResponseRedirect
from django.urls import reverse
from django.views import View
from django.views.generic import TemplateView

from apps.accounts.context import get_active_organization
from apps.permissions.authorization import get_accessible_organizations
from apps.permissions.registry import P

from . import constants as k
from . import policies, preferences_policy, registry, selectors, services
from .models import NotificationDelivery, NotificationRule, NotificationTemplate

PAGE_SIZE = 20


def _redirect_back(request: HttpRequest, fallback: str) -> HttpResponseRedirect:
    target = request.POST.get("next") or request.GET.get("next") or ""
    return HttpResponseRedirect(target if target.startswith("/") and not target.startswith("//") else fallback)


class InboxView(LoginRequiredMixin, TemplateView):
    template_name = "notifications/inbox.html"

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        user, params = self.request.user, self.request.GET
        unread = params.get("filter") == "unread"
        category = params.get("category", "")
        qs = selectors.inbox(user, unread_only=unread, category=category, include_archived=params.get("archived") == "1")
        page = Paginator(qs, PAGE_SIZE).get_page(params.get("page"))
        for notification in page.object_list:
            notification.destination = services.inapp.destination(user, notification)
        context.update(page=page, unread_count=selectors.unread_count(user), unread=unread, category=category, categories=k.Category.choices, archived=params.get("archived") == "1",
                       can_manage=policies.can_manage_anywhere(user))
        return context


class OpenView(LoginRequiredMixin, View):
    """Mark the notification read and go to its destination - only if the viewer may open that object; otherwise back to the inbox with a message."""

    def dispatch(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        if not request.user.is_authenticated:
            return super().dispatch(request, *args, **kwargs)
        notification = selectors.get_notification(request.user, kwargs["pk"])
        if notification is None:
            raise Http404
        services.mark_read(request.user, notification.pk)
        path = services.inapp.destination(request.user, notification)
        if not path:
            messages.info(request, "That item is no longer available to you.")
            return HttpResponseRedirect(reverse("notifications:inbox"))
        return HttpResponseRedirect(path)


class MarkReadView(LoginRequiredMixin, View):
    http_method_names = ["post"]

    def post(self, request: HttpRequest, pk: str) -> HttpResponse:
        try:
            services.mark_read(request.user, pk)
        except services.NotificationError as exc:
            raise Http404 from exc
        return _redirect_back(request, reverse("notifications:inbox"))


class MarkAllView(LoginRequiredMixin, View):
    http_method_names = ["post"]

    def post(self, request: HttpRequest) -> HttpResponse:
        count = services.mark_all(request.user, category=request.POST.get("category", ""))
        messages.success(request, f"{count} notification{'s' if count != 1 else ''} marked as read.")
        return _redirect_back(request, reverse("notifications:inbox"))


class ArchiveView(LoginRequiredMixin, View):
    http_method_names = ["post"]

    def post(self, request: HttpRequest, pk: str) -> HttpResponse:
        try:
            services.archive(request.user, pk)
        except services.NotificationError as exc:
            raise Http404 from exc
        return _redirect_back(request, reverse("notifications:inbox"))


class PreferencesView(LoginRequiredMixin, TemplateView):
    template_name = "notifications/preferences.html"

    def organization(self) -> Any:
        org = get_active_organization(self.request)
        if org is None:
            raise Http404
        return org

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        org = self.organization()
        mandatory = set(NotificationRule.objects.filter(organization=org, enabled=True, preference_policy=k.PreferencePolicy.MANDATORY).values_list("event_type", flat=True))
        mandatory_categories = {registry.CATEGORY_OF[e] for e in mandatory if e in registry.CATEGORY_OF}
        rows = preferences_policy.matrix(self.request.user, org)
        for row in rows:
            row["has_mandatory"] = row["category"] in mandatory_categories
        context.update(rows=rows, organization=org)
        return context

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        org = self.organization()
        try:
            for value, _label in k.Category.choices:
                if f"in_app_{value}_present" in request.POST or f"email_{value}_present" in request.POST:
                    services.set_preference(request.user, org, value, in_app_enabled=request.POST.get(f"in_app_{value}") == "on", email_enabled=request.POST.get(f"email_{value}") == "on")
        except services.NotificationError as exc:
            messages.error(request, exc.message)
        else:
            messages.success(request, "Your notification preferences were saved.")
        return HttpResponseRedirect(reverse("notifications:preferences"))


# ---------------------------------------------------------------------------------------------------------------------------------
# Administration
# ---------------------------------------------------------------------------------------------------------------------------------
class AdminMixin(LoginRequiredMixin):
    """The organizations in which the user holds ``self.permission`` (organization-wide)."""

    permission = P.NOTIFICATION_VIEW

    def dispatch(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        if request.user.is_authenticated and not self.organizations().exists():
            raise PermissionDenied
        return super().dispatch(request, *args, **kwargs)

    def organizations(self):  # noqa: ANN201
        return get_accessible_organizations(self.request.user, self.permission)

    def organization(self) -> Any:
        orgs = self.organizations()
        active = get_active_organization(self.request)
        if active is not None and orgs.filter(pk=active.pk).exists():
            return active
        found = orgs.first()
        if found is None:
            raise PermissionDenied
        return found


class RuleListView(AdminMixin, TemplateView):
    template_name = "notifications/rules.html"

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        org = self.organization()
        services.seed_defaults(org) if policies.can_configure(self.request.user, org) else None
        rules = list(NotificationRule.objects.filter(organization=org).order_by("event_type", "precedence", "code"))
        for rule in rules:
            rule.event_label = registry.EVENTS[rule.event_type].label if rule.event_type in registry.EVENTS else rule.event_type
        context.update(organization=org, rules=rules, can_edit=policies.can_configure(self.request.user, org), templates=NotificationTemplate.objects.filter(organization=org).order_by("code", "-version"))
        return context


class RuleToggleView(AdminMixin, View):
    http_method_names = ["post"]
    permission = P.NOTIFICATION_CONFIGURE

    def post(self, request: HttpRequest, pk: str) -> HttpResponse:
        rule = selectors.get_rule(request.user, pk)
        if rule is None:
            raise Http404
        try:
            services.set_rule_enabled(rule, actor=request.user, enabled=request.POST.get("action") == "enable")
        except services.NotificationError as exc:
            messages.error(request, exc.message)
        else:
            messages.success(request, "Rule updated.")
        return HttpResponseRedirect(reverse("notifications:rules"))


class DeliveryListView(AdminMixin, TemplateView):
    template_name = "notifications/deliveries.html"
    permission = P.NOTIFICATION_DELIVERIES

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        qs = selectors.deliveries_visible_to(self.request.user)
        status = self.request.GET.get("status", "")
        if status in k.DeliveryStatus.values:
            qs = qs.filter(status=status)
        channel = self.request.GET.get("channel", "")
        if channel in k.Channel.values:
            qs = qs.filter(channel=channel)
        page = Paginator(qs.order_by("-created_at"), PAGE_SIZE).get_page(self.request.GET.get("page"))
        context.update(page=page, status=status, channel=channel, statuses=k.DeliveryStatus.choices, summary=selectors.delivery_summary(self.request.user),
                       can_retry=any(policies.can_retry(self.request.user, o) for o in self.organizations()))
        return context


class DeliveryDetailView(AdminMixin, TemplateView):
    template_name = "notifications/delivery_detail.html"
    permission = P.NOTIFICATION_DELIVERIES

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        delivery = selectors.get_delivery(self.request.user, self.kwargs["pk"])
        if delivery is None:
            raise Http404
        context.update(delivery=delivery, attempts=selectors.attempts_of(delivery), can_retry=policies.can_retry(self.request.user, delivery.organization),
                       resends=NotificationDelivery.objects.filter(resend_of=delivery).order_by("created_at"))
        return context


class DeliveryActionView(AdminMixin, View):
    """Retry / resend / cancel a delivery (``notification.retry`` + a reason)."""

    http_method_names = ["post"]
    permission = P.NOTIFICATION_RETRY

    def post(self, request: HttpRequest, pk: str, action: str) -> HttpResponse:
        delivery = selectors.get_delivery(request.user, pk)
        if delivery is None:
            raise Http404
        call = {"retry": services.retry, "resend": services.resend, "cancel": services.cancel}.get(action)
        if call is None:
            raise Http404
        try:
            call(delivery, actor=request.user, reason=request.POST.get("reason", ""))
        except services.NotificationError as exc:
            messages.error(request, exc.message)
        else:
            messages.success(request, {"retry": "The delivery will be tried again.", "resend": "A new delivery was queued.", "cancel": "The delivery was cancelled."}[action])
        return HttpResponseRedirect(reverse("notifications:delivery", args=[delivery.pk]))
