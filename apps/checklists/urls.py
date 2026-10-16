"""Checklist URL configuration (mounted at ``/checklists/``).  Public identifiers are UUIDs."""

from __future__ import annotations

from django.urls import path

from . import views as v

app_name = "checklists"

urlpatterns = [
    path("", v.TemplateListView.as_view(), name="list"),
    path("create/", v.TemplateCreateView.as_view(), name="create"),
    path("<uuid:pk>/", v.TemplateDetailView.as_view(), name="detail"),
    path("<uuid:pk>/edit/", v.TemplateUpdateView.as_view(), name="edit"),
    path("<uuid:pk>/duplicate/", v.TemplateDuplicateView.as_view(), name="duplicate"),
    path("<uuid:pk>/archive/", v.TemplateStatusView.as_view(), name="status"),
    # Versions.
    path("<uuid:pk>/versions/", v.VersionListView.as_view(), name="versions"),
    path("<uuid:pk>/versions/new/", v.VersionCreateView.as_view(), name="version_new"),
    path("<uuid:pk>/versions/<int:number>/", v.VersionDetailView.as_view(), name="version_detail"),
    path("<uuid:pk>/versions/<int:number>/publish/", v.VersionPublishView.as_view(), name="version_publish"),
    # Builder (edits the draft version).
    path("<uuid:pk>/builder/", v.BuilderView.as_view(), name="builder"),
    path("<uuid:pk>/builder/sections/new/", v.SectionCreateView.as_view(), name="section_new"),
    path("<uuid:pk>/builder/sections/<uuid:section_pk>/edit/", v.SectionUpdateView.as_view(), name="section_edit"),
    path("<uuid:pk>/builder/sections/<uuid:section_pk>/delete/", v.SectionDeleteView.as_view(), name="section_delete"),
    path("<uuid:pk>/builder/sections/<uuid:section_pk>/move/", v.SectionMoveView.as_view(), name="section_move"),
    path("<uuid:pk>/builder/sections/<uuid:section_pk>/items/new/", v.ItemCreateView.as_view(), name="item_new"),
    path("<uuid:pk>/builder/answer-type/fields/", v.AnswerTypeFieldsView.as_view(), name="answer_type_fields"),
    path("<uuid:pk>/builder/answer-type/preview/", v.AnswerTypePreviewView.as_view(), name="answer_type_preview"),
    path("<uuid:pk>/builder/items/<uuid:item_pk>/edit/", v.ItemUpdateView.as_view(), name="item_edit"),
    path("<uuid:pk>/builder/items/<uuid:item_pk>/delete/", v.ItemDeleteView.as_view(), name="item_delete"),
    path("<uuid:pk>/builder/items/<uuid:item_pk>/move/", v.ItemMoveView.as_view(), name="item_move"),
    path("<uuid:pk>/builder/items/<uuid:item_pk>/options/add/", v.OptionAddView.as_view(), name="option_add"),
    path("<uuid:pk>/builder/items/<uuid:item_pk>/options/<uuid:option_pk>/edit/", v.OptionUpdateView.as_view(), name="option_edit"),
    path("<uuid:pk>/builder/items/<uuid:item_pk>/options/<uuid:option_pk>/delete/", v.OptionDeleteView.as_view(), name="option_delete"),
    path("<uuid:pk>/builder/items/<uuid:item_pk>/options/<uuid:option_pk>/move/", v.OptionMoveView.as_view(), name="option_move"),
]