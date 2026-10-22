"""Evidence routes (namespace ``evidence``)."""

from __future__ import annotations

from django.urls import path

from . import views

app_name = "evidence"

urlpatterns = [
    path("upload/", views.EvidenceUploadView.as_view(), name="upload"),
    path("attachments/<uuid:pk>/download/", views.EvidenceFileView.as_view(), name="download"),
    path("attachments/<uuid:pk>/preview/", views.EvidencePreviewView.as_view(), name="preview"),
    path("attachments/<uuid:pk>/detach/", views.EvidenceDetachView.as_view(), name="detach"),
]
