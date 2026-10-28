"""``/api/v1/dashboards/…`` (Phase 20): dashboards, the KPI catalogue, widget data and layouts.

Thin adapters over ``apps.dashboards.services`` - the same calculation path as the web widgets (which itself reads the Phase 19 report registry).  Unknown dashboards,
widgets and filters are rejected; a widget the caller may not use is a 403; scope is applied before aggregation; values are calculated on request ("live").
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
from apps.api.exceptions import StructuredValidationError
from apps.api.permissions import IsActiveUser
from apps.dashboards import catalog, kpis, policies
from apps.dashboards.services import layouts, metrics

MAX_WIDGETS_PER_REQUEST = 20


class _Strict(serializers.Serializer):
    def to_internal_value(self, data):  # noqa: ANN001, ANN201
        unknown = {key: "This field cannot be set." for key in data if key not in self.fields}
        if unknown:
            raise serializers.ValidationError(unknown)
        return super().to_internal_value(data)


class DataIn(_Strict):
    filters = serializers.DictField(required=False, default=dict)
    widgets = serializers.ListField(child=serializers.CharField(max_length=60), required=False, default=list, max_length=MAX_WIDGETS_PER_REQUEST)


class LayoutItem(_Strict):
    code = serializers.CharField(max_length=60)
    hidden = serializers.BooleanField(required=False, default=False)


class LayoutIn(_Strict):
    widgets = serializers.ListField(child=LayoutItem(), max_length=layouts.MAX_WIDGETS)


def organization(request: Request) -> Any:
    org = get_active_organization(request._request)
    if org is None or not policies.can_view(request.user, org):
        raise PermissionDenied
    return org


def dashboard_of(request: Request, org: Any, code: str) -> catalog.Dashboard:
    dashboard = catalog.DASHBOARDS.get(code)
    if dashboard is None or dashboard not in policies.visible_dashboards(request.user, org):
        raise NotFound
    return dashboard


def context_of(raw: Any) -> dict[str, Any]:
    try:
        return metrics.clean_context(raw)
    except metrics.DashboardError as exc:
        raise StructuredValidationError({"filters": [exc.message]}, extra={"code": exc.code}) from exc


def _public(context: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in context.items() if not k.startswith("_")}


def _describe(dashboard: catalog.Dashboard, user: Any, org: Any) -> dict[str, Any]:
    layout = layouts.effective(user, org, dashboard)
    return {"code": dashboard.code, "title": dashboard.title, "description": dashboard.description, "layout_source": layout["source"], "widgets": layout["widgets"]}


class DashboardListView(APIView):
    permission_classes = [IsActiveUser]

    @extend_schema(responses={200: dict}, tags=["dashboards"])
    def get(self, request: Request) -> Response:
        org = organization(request)
        return Response({"dashboards": [{"code": d.code, "title": d.title, "description": d.description} for d in policies.visible_dashboards(request.user, org)],
                         "can_manage": policies.can_manage(request.user, org)})


class KpiListView(APIView):
    permission_classes = [IsActiveUser]

    @extend_schema(responses={200: dict}, tags=["dashboards"])
    def get(self, request: Request) -> Response:
        org = organization(request)
        allowed = {w.kpi for d in policies.visible_dashboards(request.user, org) for w in policies.visible_widgets(request.user, org, d) if w.kind == catalog.KPI}
        return Response({"kpis": [kpis.describe(k) for k in kpis.REGISTRY.values() if k.code in allowed]})


class DashboardDetailView(APIView):
    permission_classes = [IsActiveUser]

    @extend_schema(responses={200: dict}, tags=["dashboards"])
    def get(self, request: Request, code: str) -> Response:
        org = organization(request)
        return Response(_describe(dashboard_of(request, org, code), request.user, org))


class DashboardDataView(APIView):
    permission_classes = [IsActiveUser]

    @extend_schema(request=DataIn, responses={200: dict}, tags=["dashboards"])
    def post(self, request: Request, code: str) -> Response:
        org = organization(request)
        dashboard = dashboard_of(request, org, code)
        data = DataIn(data=request.data)
        data.is_valid(raise_exception=True)
        context = context_of(data.validated_data["filters"])
        visible = [i["code"] for i in layouts.effective(request.user, org, dashboard)["widgets"] if not i["hidden"]]
        wanted = data.validated_data["widgets"] or visible
        unknown = [w for w in wanted if w not in dashboard.widgets]
        if unknown:
            raise StructuredValidationError({"widgets": [f"Unknown widget(s) for this dashboard: {', '.join(unknown)[:80]}."]}, extra={"code": "unknown_widget"})
        results = []
        for widget in wanted[:MAX_WIDGETS_PER_REQUEST]:
            try:
                results.append(metrics.widget_data(request.user, org, widget, context))
            except DjangoPermissionDenied:
                results.append({"code": widget, "status": "forbidden"})
        return Response({"dashboard": dashboard.code, "filters": _public(context), "period": dict(zip(("from", "to"), [d.isoformat() for d in metrics.period_of(context)])),
                         "bucket": metrics.bucket_for(context), "widgets": results})


class WidgetDataView(APIView):
    permission_classes = [IsActiveUser]

    @extend_schema(responses={200: dict}, tags=["dashboards"])
    def get(self, request: Request, widget: str) -> Response:
        org = organization(request)
        if widget not in catalog.WIDGETS:
            raise NotFound
        params = {k: v for k, v in request.query_params.items()}
        if "include_descendants" in params:
            params["include_descendants"] = params["include_descendants"] in ("1", "true")
        context = context_of(params)
        try:
            return Response(metrics.widget_data(request.user, org, widget, context))
        except DjangoPermissionDenied as exc:
            raise PermissionDenied from exc


class LayoutView(APIView):
    permission_classes = [IsActiveUser]
    shared = False

    def _run(self, call):  # noqa: ANN001, ANN202
        try:
            return call()
        except DjangoPermissionDenied as exc:
            raise PermissionDenied from exc
        except layouts.LayoutError as exc:
            raise StructuredValidationError({"widgets": [exc.message]}, extra={"code": exc.code}) from exc

    @extend_schema(responses={200: dict}, tags=["dashboards"])
    def get(self, request: Request, code: str) -> Response:
        org = organization(request)
        return Response(_describe(dashboard_of(request, org, code), request.user, org))

    @extend_schema(request=LayoutIn, responses={200: dict}, tags=["dashboards"])
    def put(self, request: Request, code: str) -> Response:
        org = organization(request)
        dashboard = dashboard_of(request, org, code)
        data = LayoutIn(data=request.data)
        data.is_valid(raise_exception=True)
        self._run(lambda: layouts.save(request.user, org, dashboard, [dict(i) for i in data.validated_data["widgets"]], shared=self.shared))
        return Response(_describe(dashboard, request.user, org))

    @extend_schema(responses={200: dict}, tags=["dashboards"])
    def delete(self, request: Request, code: str) -> Response:
        org = organization(request)
        dashboard = dashboard_of(request, org, code)
        self._run(lambda: layouts.reset(request.user, org, dashboard, shared=self.shared))
        return Response(_describe(dashboard, request.user, org))


class SharedLayoutView(LayoutView):
    shared = True
