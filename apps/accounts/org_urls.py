"""Organization-context URLs (mounted at ``/organizations/``).

Kept in the accounts app because the "current organization" is an *identity*
concern, but exposed under the ``/organizations/`` namespace the spec suggests.
This urlconf is included before ``apps.organizations.urls``; non-matching paths
fall through to the organization domain routes.
"""

from __future__ import annotations

from django.urls import path

from .views import OrganizationSelectView, OrganizationSwitchView

app_name = "orgcontext"

urlpatterns = [
    path("select/", OrganizationSelectView.as_view(), name="select"),
    path("switch/", OrganizationSwitchView.as_view(), name="switch"),
]
