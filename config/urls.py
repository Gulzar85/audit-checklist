"""Root URL configuration.

Web pages live under CBVs (see ``apps.core`` / ``apps.accounts``), the versioned
REST API is mounted at ``/api/v1/`` and the OpenAPI schema at ``/api/schema/``.
"""

from __future__ import annotations

from django.apps import apps
from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.urls import include, path

from apps.api.schema import APISchemaView, APISwaggerView

urlpatterns = [
    path("admin/", admin.site.urls),
]

# ``django.contrib.admindocs`` is only installed in some environments (e.g.
# development). When it is, Jazzmin renders a link to its index, which needs the
# ``django-admindocs-docroot`` URL name to reverse successfully.
if apps.is_installed("django.contrib.admindocs"):
    urlpatterns += [
        path("admin/doc/", include("django.contrib.admindocs.urls")),
    ]

urlpatterns += [
    path("", include("apps.core.urls", namespace="core")),
    path("auth/", include("apps.accounts.urls", namespace="accounts")),
    # Organization context (identity concern) shares the /organizations/ prefix;
    # it is matched first, and unmatched paths fall through to the domain routes.
    path("organizations/", include("apps.accounts.org_urls", namespace="orgcontext")),
    path("organizations/", include("apps.organizations.urls", namespace="organizations")),
    path("locations/", include("apps.locations.urls", namespace="locations")),
    path("themes/", include("apps.themes.urls", namespace="themes")),
    path("access/", include("apps.permissions.urls", namespace="access")),
    path("checklists/", include("apps.checklists.urls", namespace="checklists")),
    path("assignments/", include("apps.assignments.urls", namespace="assignments")),
    path("executions/", include("apps.executions.urls", namespace="executions")),
    path("findings/", include("apps.findings.urls", namespace="findings")),
    path("evidence/", include("apps.evidence.urls", namespace="evidence")),
    # Versioned REST API.
    path("api/v1/", include(("apps.api.v1.urls", "v1"), namespace="v1")),
    # API documentation / schema.
    path("api/schema/", APISchemaView.as_view(), name="api-schema"),
    path("api/docs/", APISwaggerView.as_view(url_name="api-schema"), name="api-docs"),
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
    urlpatterns += static(settings.STATIC_URL, document_root=settings.STATIC_ROOT)

