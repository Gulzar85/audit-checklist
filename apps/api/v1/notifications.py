"""``/api/v1/notifications/…`` (Phase 17): the user's inbox and preferences, and the administrative rules / templates / deliveries.

Thin adapters over ``apps.notifications``: visibility comes from the selectors (a notification of another user, or of an organization the caller cannot administer, is a 404),
authorization from the services (403) and every write from the same services the web UI uses.  There is no endpoint that creates a notification, a delivery or an attempt, or
that marks a delivery sent / delivered: those facts are produced by the server.  Unknown fields are rejected.  Email bodies and addresses are never returned; a notification's
destination is computed for the caller and omitted when they may not open the object.
"""

from __future__ import annotations

from typing import Any

from django.core.exceptions import PermissionDenied as DjangoPermissionDenied
from drf_spectacular.utils import extend_schema
from rest_framework import serializers
from rest_framework.exceptions import NotFound, PermissionDenied
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.accounts.context import get_active_organization
from apps.api.exceptions import ConflictAPIException, StructuredValidationError
from apps.api.permissions import IsActiveUser
from apps.notifications import constants as k
from apps.notifications import policies, preferences_policy, registry, selectors, services
from apps.notifications.models import Notification, NotificationDelivery, NotificationRule, NotificationTemplate

PAGE_MAX = 100


def run_service(call):  # noqa: ANN001, ANN201
    try:
        return call()
    except DjangoPermissionDenied as exc:
        raise PermissionDenied from exc
    except services.NotificationError as exc:
        if isinstance(exc, services.NotificationConflict):
            raise ConflictAPIException(detail={"detail": exc.message, "code": exc.code}) from exc
        raise StructuredValidationError({"__all__": [exc.message]}, extra={"code": exc.code, **exc.extra}) from exc


class _Strict(serializers.Serializer):
    def to_internal_value(self, data):  # noqa: ANN001, ANN201
        unknown = {key: "This field cannot be set." for key in data if key not in self.fields}
        if unknown:
            raise serializers.ValidationError(unknown)
        return super().to_internal_value(data)


def _page(request: Request, qs):  # noqa: ANN001, ANN202
    try:
        limit = max(1, min(int(request.query_params.get("limit", 25)), PAGE_MAX))
        offset = max(0, int(request.query_params.get("offset", 0)))
    except ValueError as exc:
        raise StructuredValidationError({"limit": ["Use whole numbers for limit and offset."]}) from exc
    total = qs.count()
    return total, list(qs[offset:offset + limit]), limit, offset


def _notification(user: Any, n: Notification) -> dict[str, Any]:
    return {"id": str(n.pk), "event_type": n.event_type, "category": n.category, "priority": n.priority, "title": n.title, "message": n.message, "is_read": n.is_read,
            "read_at": n.read_at, "archived_at": n.archived_at, "created_at": n.created_at, "target_type": n.target_type, "target_id": str(n.target_id),
            "destination": services.inapp.destination(user, n) or None}


def _rule(r: NotificationRule) -> dict[str, Any]:
    return {"id": str(r.pk), "code": r.code, "name": r.name, "description": r.description, "event_type": r.event_type, "enabled": r.enabled, "is_system_default": r.is_system_default,
            "precedence": r.precedence, "recipient_strategies": r.recipient_strategies, "channels": r.channels, "template_code": r.template_code, "priority": r.priority,
            "preference_policy": r.preference_policy, "filters": r.filters, "exclude_actor": r.exclude_actor, "revision": r.revision, "organization": str(r.organization_id)}


def _template(t: NotificationTemplate) -> dict[str, Any]:
    return {"id": str(t.pk), "code": t.code, "name": t.name, "event_type": t.event_type, "locale": t.locale, "status": t.status, "version": t.version, "subject": t.subject,
            "body_text": t.body_text, "body_html": t.body_html, "in_app_title": t.in_app_title, "in_app_message": t.in_app_message, "organization": str(t.organization_id) if t.organization_id else None}


def _delivery(d: NotificationDelivery) -> dict[str, Any]:
    return {"id": str(d.pk), "notification": str(d.notification_id), "channel": d.channel, "status": d.status, "provider": d.provider, "to": d.to_masked, "attempt_count": d.attempt_count,
            "max_attempts": d.max_attempts, "next_attempt_at": d.next_attempt_at, "accepted_at": d.accepted_at, "delivered_at": d.delivered_at, "last_error_category": d.last_error_category,
            "last_error": d.last_error, "suppressed_reason": d.suppressed_reason, "resend_of": str(d.resend_of_id) if d.resend_of_id else None, "created_at": d.created_at}


def _organization(request: Request, user: Any, check) -> Any:  # noqa: ANN001
    """The organization of the active request context in which ``check(user, org)`` holds."""
    org = get_active_organization(request._request)
    if org is None or not check(user, org):
        raise PermissionDenied
    return org


class MetadataView(APIView):
    permission_classes = [IsActiveUser]

    @extend_schema(responses={200: dict}, tags=["notifications"])
    def get(self, request: Request) -> Response:
        return Response({**registry.describe_all(), "categories": [{"value": v, "label": str(label)} for v, label in k.Category.choices],
                         "policies": [{"value": v, "label": str(label)} for v, label in k.PreferencePolicy.choices]})


class NotificationListView(APIView):
    permission_classes = [IsActiveUser]

    @extend_schema(responses={200: dict}, tags=["notifications"])
    def get(self, request: Request) -> Response:
        params = request.query_params
        qs = selectors.inbox(request.user, unread_only=params.get("unread") in {"1", "true"}, category=params.get("category", ""), include_archived=params.get("archived") in {"1", "true"})
        total, rows, limit, offset = _page(request, qs)
        return Response({"count": total, "limit": limit, "offset": offset, "unread": selectors.unread_count(request.user), "results": [_notification(request.user, n) for n in rows]})


class NotificationDetailView(APIView):
    permission_classes = [IsActiveUser]

    @extend_schema(responses={200: dict}, tags=["notifications"])
    def get(self, request: Request, pk: str) -> Response:
        n = selectors.get_notification(request.user, pk)
        if n is None:
            raise NotFound
        return Response(_notification(request.user, n))


class UnreadCountView(APIView):
    permission_classes = [IsActiveUser]

    @extend_schema(responses={200: dict}, tags=["notifications"])
    def get(self, request: Request) -> Response:
        return Response({"unread": selectors.unread_count(request.user)})


class MarkReadView(APIView):
    permission_classes = [IsActiveUser]

    @extend_schema(request=None, responses={200: dict}, tags=["notifications"])
    def post(self, request: Request, pk: str) -> Response:
        if request.data:
            raise StructuredValidationError({"__all__": ["This request takes no body."]})
        n = run_service(lambda: services.mark_read(request.user, pk)) if selectors.get_notification(request.user, pk) else None
        if n is None:
            raise NotFound
        return Response(_notification(request.user, n))


class ArchiveView(APIView):
    permission_classes = [IsActiveUser]

    @extend_schema(request=None, responses={200: dict}, tags=["notifications"])
    def post(self, request: Request, pk: str) -> Response:
        if selectors.get_notification(request.user, pk) is None:
            raise NotFound
        return Response(_notification(request.user, run_service(lambda: services.archive(request.user, pk))))


class BulkReadIn(_Strict):
    ids = serializers.ListField(child=serializers.UUIDField(), required=False, max_length=k.BULK_LIMIT)
    all = serializers.BooleanField(required=False, default=False)
    category = serializers.ChoiceField(choices=k.Category.choices, required=False, allow_blank=True, default="")


class BulkReadView(APIView):
    permission_classes = [IsActiveUser]

    @extend_schema(request=BulkReadIn, responses={200: dict}, tags=["notifications"])
    def post(self, request: Request) -> Response:
        data = BulkReadIn(data=request.data)
        data.is_valid(raise_exception=True)
        body = data.validated_data
        if body.get("all"):
            count = services.mark_all(request.user, category=body.get("category", ""))
        elif body.get("ids"):
            count = run_service(lambda: services.mark_many(request.user, [str(i) for i in body["ids"]]))
        else:
            raise StructuredValidationError({"__all__": ["Give either ids or all=true."]})
        return Response({"marked": count, "unread": selectors.unread_count(request.user)})


class PreferenceIn(_Strict):
    category = serializers.ChoiceField(choices=k.Category.choices)
    in_app_enabled = serializers.BooleanField(required=False)
    email_enabled = serializers.BooleanField(required=False)


class PreferencesIn(_Strict):
    preferences = serializers.ListField(child=PreferenceIn(), max_length=len(k.Category.choices))


class PreferencesView(APIView):
    permission_classes = [IsActiveUser]

    def _org(self, request: Request) -> Any:
        org = get_active_organization(request._request)
        if org is None:
            raise NotFound
        return org

    @extend_schema(responses={200: dict}, tags=["notifications"])
    def get(self, request: Request) -> Response:
        org = self._org(request)
        return Response({"organization": str(org.pk), "preferences": preferences_policy.matrix(request.user, org)})

    @extend_schema(request=PreferencesIn, responses={200: dict}, tags=["notifications"])
    def put(self, request: Request) -> Response:
        org = self._org(request)
        data = PreferencesIn(data=request.data)
        data.is_valid(raise_exception=True)
        for item in data.validated_data["preferences"]:
            run_service(lambda item=item: services.set_preference(request.user, org, item["category"], in_app_enabled=item.get("in_app_enabled"), email_enabled=item.get("email_enabled")))
        return Response({"organization": str(org.pk), "preferences": preferences_policy.matrix(request.user, org)})


# ----------------------------------------------------------------------------------------------------------------------- administration
class RuleIn(_Strict):
    name = serializers.CharField(max_length=120, required=False)
    description = serializers.CharField(max_length=500, required=False, allow_blank=True)
    event_type = serializers.CharField(max_length=60, required=False)
    enabled = serializers.BooleanField(required=False)
    precedence = serializers.IntegerField(min_value=0, max_value=10000, required=False)
    recipient_strategies = serializers.ListField(child=serializers.DictField(), required=False, max_length=10)
    channels = serializers.ListField(child=serializers.CharField(max_length=10), required=False, max_length=4)
    template_code = serializers.CharField(max_length=60, required=False, allow_blank=True)
    priority = serializers.CharField(max_length=8, required=False)
    preference_policy = serializers.CharField(max_length=12, required=False)
    filters = serializers.ListField(child=serializers.DictField(), required=False, max_length=10)
    exclude_actor = serializers.BooleanField(required=False)


class RuleCreateIn(RuleIn):
    code = serializers.SlugField(max_length=60)


class RuleListView(APIView):
    permission_classes = [IsActiveUser]

    @extend_schema(responses={200: dict}, tags=["notifications"])
    def get(self, request: Request) -> Response:
        qs = selectors.rules_visible_to(request.user).order_by("event_type", "precedence", "code")
        if request.query_params.get("event_type"):
            qs = qs.filter(event_type=request.query_params["event_type"])
        total, rows, limit, offset = _page(request, qs)
        return Response({"count": total, "limit": limit, "offset": offset, "results": [_rule(r) for r in rows]})

    @extend_schema(request=RuleCreateIn, responses={201: dict}, tags=["notifications"])
    def post(self, request: Request) -> Response:
        org = _organization(request, request.user, policies.can_configure)
        data = RuleCreateIn(data=request.data)
        data.is_valid(raise_exception=True)
        body = dict(data.validated_data)
        code = body.pop("code")
        rule = run_service(lambda: services.save_rule(org, actor=request.user, data=body, code=code))
        return Response(_rule(rule), status=201)


class RuleDetailView(APIView):
    permission_classes = [IsActiveUser]

    @extend_schema(responses={200: dict}, tags=["notifications"])
    def get(self, request: Request, pk: str) -> Response:
        rule = selectors.get_rule(request.user, pk)
        if rule is None:
            raise NotFound
        return Response(_rule(rule))

    @extend_schema(request=RuleIn, responses={200: dict}, tags=["notifications"])
    def put(self, request: Request, pk: str) -> Response:
        rule = selectors.get_rule(request.user, pk)
        if rule is None:
            raise NotFound
        data = RuleIn(data=request.data)
        data.is_valid(raise_exception=True)
        return Response(_rule(run_service(lambda: services.save_rule(rule.organization, actor=request.user, data=dict(data.validated_data), rule=rule))))


class RuleToggleView(APIView):
    permission_classes = [IsActiveUser]

    @extend_schema(request=None, responses={200: dict}, tags=["notifications"])
    def post(self, request: Request, pk: str, action: str) -> Response:
        rule = selectors.get_rule(request.user, pk)
        if rule is None or action not in {"enable", "disable"}:
            raise NotFound
        return Response(_rule(run_service(lambda: services.set_rule_enabled(rule, actor=request.user, enabled=action == "enable"))))


class TemplateIn(_Strict):
    name = serializers.CharField(max_length=120)
    event_type = serializers.CharField(max_length=60)
    locale = serializers.CharField(max_length=10, required=False, default="en")
    subject = serializers.CharField(max_length=200, required=False, allow_blank=True, default="")
    body_text = serializers.CharField(max_length=5000, required=False, allow_blank=True, default="")
    body_html = serializers.CharField(max_length=10000, required=False, allow_blank=True, default="")
    in_app_title = serializers.CharField(max_length=160, required=False, allow_blank=True, default="")
    in_app_message = serializers.CharField(max_length=500, required=False, allow_blank=True, default="")


class TemplateCreateIn(TemplateIn):
    code = serializers.SlugField(max_length=60)


FIELD_NAMES = ("subject", "body_text", "body_html", "in_app_title", "in_app_message")


class TemplateListView(APIView):
    permission_classes = [IsActiveUser]

    @extend_schema(responses={200: dict}, tags=["notifications"])
    def get(self, request: Request) -> Response:
        total, rows, limit, offset = _page(request, selectors.templates_visible_to(request.user).order_by("code", "-version"))
        return Response({"count": total, "limit": limit, "offset": offset, "results": [_template(t) for t in rows]})

    @extend_schema(request=TemplateCreateIn, responses={201: dict}, tags=["notifications"])
    def post(self, request: Request) -> Response:
        org = _organization(request, request.user, policies.can_configure)
        data = TemplateCreateIn(data=request.data)
        data.is_valid(raise_exception=True)
        body = data.validated_data
        template = run_service(lambda: services.save_template(org, actor=request.user, code=body["code"], name=body["name"], event_type=body["event_type"], locale=body["locale"],
                                                              fields={f: body[f] for f in FIELD_NAMES}))
        return Response(_template(template), status=201)


class TemplateActionView(APIView):
    permission_classes = [IsActiveUser]

    @extend_schema(request=None, responses={200: dict}, tags=["notifications"])
    def post(self, request: Request, pk: str, action: str) -> Response:
        template = selectors.get_template(request.user, pk)
        call = {"activate": services.activate_template, "archive": services.archive_template}.get(action)
        if template is None or call is None:
            raise NotFound
        return Response(_template(run_service(lambda: call(template, actor=request.user))))


class PreviewIn(_Strict):
    event_type = serializers.CharField(max_length=60)
    subject = serializers.CharField(max_length=200, required=False, allow_blank=True, default="")
    body_text = serializers.CharField(max_length=5000, required=False, allow_blank=True, default="")
    body_html = serializers.CharField(max_length=10000, required=False, allow_blank=True, default="")
    in_app_title = serializers.CharField(max_length=160, required=False, allow_blank=True, default="")
    in_app_message = serializers.CharField(max_length=500, required=False, allow_blank=True, default="")


class TemplatePreviewView(APIView):
    """Render a draft with synthetic sample values (nothing is stored or sent)."""

    permission_classes = [IsActiveUser]

    @extend_schema(request=PreviewIn, responses={200: dict}, tags=["notifications"])
    def post(self, request: Request) -> Response:
        if not policies.can_manage_anywhere(request.user):
            raise PermissionDenied
        data = PreviewIn(data=request.data)
        data.is_valid(raise_exception=True)
        body = data.validated_data
        return Response(run_service(lambda: services.preview(body["event_type"], {f: body[f] for f in FIELD_NAMES})))


class DeliveryListView(APIView):
    permission_classes = [IsActiveUser]

    @extend_schema(responses={200: dict}, tags=["notifications"])
    def get(self, request: Request) -> Response:
        qs = selectors.deliveries_visible_to(request.user).order_by("-created_at")
        params = request.query_params
        if params.get("status") in k.DeliveryStatus.values:
            qs = qs.filter(status=params["status"])
        if params.get("channel") in k.Channel.values:
            qs = qs.filter(channel=params["channel"])
        total, rows, limit, offset = _page(request, qs)
        return Response({"count": total, "limit": limit, "offset": offset, "summary": selectors.delivery_summary(request.user), "results": [_delivery(d) for d in rows]})


class DeliveryDetailView(APIView):
    permission_classes = [IsActiveUser]

    @extend_schema(responses={200: dict}, tags=["notifications"])
    def get(self, request: Request, pk: str) -> Response:
        d = selectors.get_delivery(request.user, pk)
        if d is None:
            raise NotFound
        attempts = [{"number": a.number, "outcome": a.outcome, "error_category": a.error_category, "provider_status": a.provider_status, "duration_ms": a.duration_ms, "started_at": a.started_at,
                     "finished_at": a.finished_at} for a in selectors.attempts_of(d)]
        return Response({**_delivery(d), "attempts": attempts})


class ReasonIn(_Strict):
    reason = serializers.CharField(max_length=300)


class DeliveryActionView(APIView):
    """Retry / resend / cancel: ``notification.retry`` and a reason (audited)."""

    permission_classes = [IsActiveUser]

    @extend_schema(request=ReasonIn, responses={200: dict}, tags=["notifications"])
    def post(self, request: Request, pk: str, action: str) -> Response:
        d = selectors.get_delivery(request.user, pk)
        call = {"retry": services.retry, "resend": services.resend, "cancel": services.cancel}.get(action)
        if d is None or call is None:
            raise NotFound
        data = ReasonIn(data=request.data)
        data.is_valid(raise_exception=True)
        return Response(_delivery(run_service(lambda: call(d, actor=request.user, reason=data.validated_data["reason"]))))
