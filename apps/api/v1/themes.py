"""``/api/v1/themes/`` and ``/api/v1/preferences/appearance/`` - the theme engine API.

A thin adapter, like the other v1 resources: the queryset is the selector
(``themes_visible_to`` - the same tenant isolation as the web UI), authorization is the policy
layer, writes are the service layer.  There is no DELETE: themes are archived.
"""

from __future__ import annotations

from typing import Any

from django.core.exceptions import PermissionDenied
from drf_spectacular.utils import extend_schema
from rest_framework import mixins, status, viewsets
from rest_framework.decorators import action
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.api.permissions import IsActiveUser, PolicyPermission
from apps.themes import contrast, services
from apps.themes import tokens as tk
from apps.themes.filters import ThemeFilter
from apps.themes.models import Theme, UserAppearance
from apps.themes.policies import (
    can_activate_theme,
    can_archive_theme,
    can_clone_theme,
    can_create_theme,
    can_export_theme,
    can_import_theme,
    can_manage_theme,
    can_preview_theme,
    can_publish_theme,
    can_rollback_theme,
)
from apps.themes.resolution import effective_tokens_for, resolve_theme
from apps.themes.selectors import themes_visible_to
from apps.themes.serializers import (
    AppearanceSerializer,
    ThemeCloneSerializer,
    ThemeCreateSerializer,
    ThemeImportSerializer,
    ThemePreviewSerializer,
    ThemeSerializer,
    ThemeUpdateSerializer,
)


def _checks(tokens: dict) -> list[dict[str, Any]]:
    return [
        {
            "mode": c.mode,
            "label": c.label,
            "foreground": c.foreground,
            "background": c.background,
            "ratio": round(c.ratio, 2),
            "required": c.required,
            "level": c.level,
        }
        for c in contrast.contrast_checks(tokens)
    ]


class ThemeViewSet(
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    mixins.CreateModelMixin,
    mixins.UpdateModelMixin,
    viewsets.GenericViewSet,
):
    """Themes the caller may see (system themes + their organizations' themes)."""

    serializer_class = ThemeSerializer
    permission_classes = [IsActiveUser, PolicyPermission]
    filterset_class = ThemeFilter
    search_fields = ("name", "code")
    ordering_fields = ("name", "code", "status", "version", "updated_at")
    ordering = ("scope", "name", "-version")

    def get_queryset(self):  # noqa: ANN201
        return themes_visible_to(self.request.user)

    # -- authorization (policy layer) ----------------------------------------------------------
    def policy_allows_collection(self, request: Request) -> bool:
        if self.action in ("create", "import_theme"):
            return request.user.has_perm("themes.add_theme")
        return True

    def policy_allows_object(self, request: Request, obj: Theme) -> bool:
        user = request.user
        checks = {
            "publish": can_publish_theme,
            "activate": can_activate_theme,
            "rollback": can_rollback_theme,
            "archive": can_archive_theme,
            "new_version": lambda u, t: can_manage_theme(u, t) and not t.is_draft,
            "clone": can_clone_theme,
            "preview": can_preview_theme,
            "export": can_export_theme,
        }
        if self.action in checks:
            return checks[self.action](user, obj)
        if request.method in ("GET", "HEAD", "OPTIONS"):
            return True
        return can_manage_theme(user, obj)

    def get_serializer_class(self):  # noqa: ANN201
        return {
            "create": ThemeCreateSerializer,
            "update": ThemeUpdateSerializer,
            "partial_update": ThemeUpdateSerializer,
        }.get(self.action, ThemeSerializer)

    # -- create / update (service layer) ----------------------------------------------------------
    @extend_schema(request=ThemeCreateSerializer, responses=ThemeSerializer)
    def create(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        payload = ThemeCreateSerializer(data=request.data, context=self.get_serializer_context())
        payload.is_valid(raise_exception=True)
        data = dict(payload.validated_data)
        organization = data.pop("organization", None)
        if not can_create_theme(request.user, organization):
            raise PermissionDenied
        theme = services.create_theme(actor=request.user, organization=organization, **data)
        return Response(ThemeSerializer(theme).data, status=status.HTTP_201_CREATED)

    @extend_schema(request=ThemeUpdateSerializer, responses=ThemeSerializer)
    def update(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        partial = kwargs.pop("partial", False)
        theme = self.get_object()
        payload = ThemeUpdateSerializer(data=request.data, partial=partial, context=self.get_serializer_context())
        payload.is_valid(raise_exception=True)
        data = dict(payload.validated_data)
        theme = services.update_theme(theme, actor=request.user, **data)
        return Response(ThemeSerializer(theme).data)

    # -- lifecycle -----------------------------------------------------------------------------------
    def _transition(self, request: Request, operation) -> Response:  # noqa: ANN001
        theme = operation(self.get_object(), actor=request.user)
        return Response(ThemeSerializer(theme).data)

    @extend_schema(request=None, responses=ThemeSerializer)
    @action(detail=True, methods=["post"])
    def publish(self, request: Request, pk: Any = None) -> Response:
        """draft -> published (blocked while mandatory WCAG contrast fails)."""
        return self._transition(request, services.publish_theme)

    @extend_schema(request=None, responses=ThemeSerializer)
    @action(detail=True, methods=["post"])
    def activate(self, request: Request, pk: Any = None) -> Response:
        """published -> active for the theme's organization (or system-wide)."""
        return self._transition(request, services.activate_theme)

    @extend_schema(request=None, responses=ThemeSerializer)
    @action(detail=True, methods=["post"])
    def rollback(self, request: Request, pk: Any = None) -> Response:
        """Re-activate the previously active theme.  Call on the *currently active* theme."""
        return self._transition(request, services.rollback_theme)

    @extend_schema(request=None, responses=ThemeSerializer)
    @action(detail=True, methods=["post"])
    def archive(self, request: Request, pk: Any = None) -> Response:
        return self._transition(request, services.archive_theme)

    @extend_schema(request=None, responses=ThemeSerializer)
    @action(detail=True, methods=["post"], url_path="new-version")
    def new_version(self, request: Request, pk: Any = None) -> Response:
        """A new draft version (same code, version + 1) of a published theme."""
        theme = services.create_new_version(self.get_object(), actor=request.user)
        return Response(ThemeSerializer(theme).data, status=status.HTTP_201_CREATED)

    @extend_schema(request=ThemeCloneSerializer, responses=ThemeSerializer)
    @action(detail=True, methods=["post"])
    def clone(self, request: Request, pk: Any = None) -> Response:
        source = self.get_object()
        payload = ThemeCloneSerializer(data=request.data, context=self.get_serializer_context())
        payload.is_valid(raise_exception=True)
        kwargs: dict[str, Any] = {"name": payload.validated_data.get("name", "")}
        if "organization" in payload.validated_data:
            kwargs["organization"] = payload.validated_data["organization"]
            if not can_clone_theme(request.user, source, kwargs["organization"]):
                raise PermissionDenied
        clone = services.clone_theme(source, actor=request.user, **kwargs)
        return Response(ThemeSerializer(clone).data, status=status.HTTP_201_CREATED)

    # -- preview / export / import ---------------------------------------------------------------------
    @extend_schema(request=ThemePreviewSerializer, methods=["POST"])
    @action(detail=True, methods=["get", "post"])
    def preview(self, request: Request, pk: Any = None) -> Response:
        """Preview a theme (GET) or the theme with *unsaved* token overrides (POST).  Stores nothing."""
        theme = self.get_object()
        mode = "light"
        tokens = effective_tokens_for(theme)
        if request.method == "POST":
            payload = ThemePreviewSerializer(data=request.data, context=self.get_serializer_context())
            payload.is_valid(raise_exception=True)
            mode = payload.validated_data["mode"]
            if "tokens" in payload.validated_data:
                tokens = tk.merge_tokens(_system_layer(theme), payload.validated_data["tokens"])
        return Response(
            {
                "mode": mode,
                "variables": tk.preview_variables(tokens, mode),
                "accessibility": _checks(tokens),
                "publishable": not contrast.mandatory_failures(tokens),
            }
        )

    @extend_schema(responses={200: dict})
    @action(detail=True, methods=["get"])
    def export(self, request: Request, pk: Any = None) -> Response:
        return Response(services.export_theme(self.get_object()))

    @extend_schema(request=ThemeImportSerializer, responses=ThemeSerializer)
    @action(detail=False, methods=["post"], url_path="import")
    def import_theme(self, request: Request) -> Response:
        payload = ThemeImportSerializer(data=request.data, context=self.get_serializer_context())
        payload.is_valid(raise_exception=True)
        organization = payload.validated_data.get("organization")
        if not can_import_theme(request.user, organization):
            raise PermissionDenied
        theme = services.import_theme(payload.validated_data["document"], actor=request.user, organization=organization)
        return Response(ThemeSerializer(theme).data, status=status.HTTP_201_CREATED)

    @extend_schema(responses={200: dict})
    @action(detail=False, methods=["get"])
    def active(self, request: Request) -> Response:
        """The look this caller currently gets: precedence applied, ready to render."""
        resolved = resolve_theme(request._request)
        return Response(
            {
                "mode": resolved.mode,
                "source": resolved.source,
                "theme": (
                    {"id": str(resolved.theme.pk), "name": resolved.theme.name, "code": resolved.theme.code, "version": resolved.theme.version}
                    if resolved.theme
                    else None
                ),
                "stylesheet": resolved.css_url,
                "variables": {
                    "light": tk.preview_variables(resolved.tokens, "light"),
                    "dark": tk.preview_variables(resolved.tokens, "dark"),
                },
                "pwa": {"light": resolved.pwa("light"), "dark": resolved.pwa("dark")},
            }
        )


def _system_layer(theme: Theme) -> dict | None:
    from apps.themes.selectors import get_active_themes

    if theme.organization_id is None:
        return None
    system_theme = get_active_themes(None)[0]
    return system_theme.tokens if system_theme and system_theme.pk != theme.pk else None


class AppearancePreferenceView(APIView):
    """``/api/v1/preferences/appearance/`` - the caller's light / dark / system choice."""

    permission_classes = [IsActiveUser]

    @extend_schema(responses=AppearanceSerializer)
    def get(self, request: Request) -> Response:
        stored = UserAppearance.objects.filter(user=request.user).values_list("mode", flat=True).first()
        return Response({"mode": stored, "effective": resolve_theme(request._request).mode})

    @extend_schema(request=AppearanceSerializer, responses=AppearanceSerializer)
    def put(self, request: Request) -> Response:
        payload = AppearanceSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        services.set_user_appearance(request.user, payload.validated_data["mode"])
        # Resolve afresh: the request-scoped memo (if any) predates the change.
        stored = UserAppearance.objects.filter(user=request.user).values_list("mode", flat=True).first()
        return Response({"mode": stored})

    patch = put
