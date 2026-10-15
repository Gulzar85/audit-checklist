"""Theme engine URLs (mounted at ``/themes/``)."""

from __future__ import annotations

from django.urls import path

from . import views

app_name = "themes"

urlpatterns = [
    # Public: the active theme as CSS custom properties, and the appearance switch.
    path("tokens.css", views.ThemeStylesheetView.as_view(), name="tokens"),
    path("appearance/", views.AppearanceView.as_view(), name="appearance"),
    # Management.
    path("", views.ThemeListView.as_view(), name="list"),
    path("new/", views.ThemeCreateView.as_view(), name="create"),
    path("import/", views.ThemeImportView.as_view(), name="import"),
    path("gallery/", views.ThemeGuideView.as_view(), name="gallery"),
    path("preview/stop/", views.ThemePreviewStopView.as_view(), name="preview_stop"),
    path("<uuid:pk>/", views.ThemeDetailView.as_view(), name="detail"),
    path("<uuid:pk>/edit/", views.ThemeEditView.as_view(), name="edit"),
    path("<uuid:pk>/preview/", views.ThemePreviewFragmentView.as_view(), name="preview"),
    path("<uuid:pk>/try/", views.ThemeTryView.as_view(), name="try"),
    path("<uuid:pk>/publish/", views.ThemePublishView.as_view(), name="publish"),
    path("<uuid:pk>/activate/", views.ThemeActivateView.as_view(), name="activate"),
    path("<uuid:pk>/rollback/", views.ThemeRollbackView.as_view(), name="rollback"),
    path("<uuid:pk>/archive/", views.ThemeArchiveView.as_view(), name="archive"),
    path("<uuid:pk>/new-version/", views.ThemeNewVersionView.as_view(), name="new_version"),
    path("<uuid:pk>/clone/", views.ThemeCloneView.as_view(), name="clone"),
    path("<uuid:pk>/export/", views.ThemeExportView.as_view(), name="export"),
]
