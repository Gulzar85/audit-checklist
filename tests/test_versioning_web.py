"""The version screens: history, detail, builder controls, validate / publish / discard / draft-from, compare and preview."""

from __future__ import annotations

import pytest
from django.urls import reverse

from apps.checklists import lifecycle, services
from apps.checklists.models import ChecklistVersion
from apps.core.models import ActivityLog

from versioning_support import act, build_world, item_of

HX = {"HTTP_HX_REQUEST": "true"}


def body(response) -> str:
    return response.content.decode()


def url(name, *args):
    return reverse(f"checklists:{name}", args=args)


@pytest.fixture
def world(org_a, org_b, make_user, assign):
    return build_world(org_a, org_b, make_user, assign)


@pytest.fixture
def published(org_a, org_b, make_user, assign):
    return build_world(org_a, org_b, make_user, assign, publish=True)


def evolve(world):
    author = world["author"]
    v2 = lifecycle.create_draft(world["template"], actor=author, change_summary="Tighten range")
    services.update_item(item_of(v2, "FRIDGE-TEMP"), actor=author, config={"min": 0, "max": 8, "decimals": 1, "unit": "°C"})
    services.update_item(item_of(v2, "FIRE-EXIT"), actor=author, question="Is every fire exit clear?")
    lifecycle.publish_version(v2, actor=author, change_summary="Tighten range")
    return v2


@pytest.mark.django_db
class TestHistory:
    def test_the_history_shows_status_people_dates_and_structure(self, client, published):
        v2 = lifecycle.create_draft(published["template"], actor=published["author"], change_summary="Next release")
        client.force_login(published["reader"])
        html = body(client.get(url("versions", published["template"].pk)))
        assert "Version history" in html and "Next release" in html and "from v1" in html
        for needle in ("Draft", "Published", "3 sections", "5 items", "vs-author" if False else "Initial version"):
            assert needle in html
        assert url("version_detail", published["template"].pk, 1) in html and url("version_preview", published["template"].pk, 2) in html
        assert url("compare", published["template"].pk) in html and v2.pk

    def test_statuses_are_text_and_icon_never_colour_alone(self, client, published):
        evolve(published)
        client.force_login(published["reader"])
        html = body(client.get(url("versions", published["template"].pk)))
        assert "Superseded" in html and "Published" in html and 'data-lucide="history"' in html and "replaced by a newer version" in html

    def test_a_reader_sees_no_create_or_draft_buttons(self, client, published):
        client.force_login(published["reader"])
        html = body(client.get(url("versions", published["template"].pk)))
        assert url("version_new", published["template"].pk) not in html and url("version_draft_from", published["template"].pk, 1) not in html

    def test_an_editor_can_start_a_draft_from_a_version(self, client, published):
        client.force_login(published["editor"])
        html = body(client.get(url("versions", published["template"].pk)))
        assert url("version_draft_from", published["template"].pk, 1) in html and "New draft from the current version (v1)" in html

    def test_long_histories_are_paginated(self, client, published):
        author = published["author"]
        for _ in range(21):
            lifecycle.publish_version(lifecycle.create_draft(published["template"], actor=author), actor=author)
        client.force_login(author)
        first = body(client.get(url("versions", published["template"].pk)))
        second = body(client.get(url("versions", published["template"].pk), {"page": 2}))
        assert 'font-medium">v22' in first and 'font-medium">v1</td>' not in first and "Page 1 of 2" in first and "Older" in first
        assert 'font-medium">v1</td>' in second and 'font-medium">v22' not in second and "Newer" in second

    def test_the_history_query_count_is_flat(self, client, published, django_assert_max_num_queries):
        author = published["author"]
        for _ in range(4):
            lifecycle.publish_version(lifecycle.create_draft(published["template"], actor=author), actor=author)
        client.force_login(author)
        client.get(url("versions", published["template"].pk))  # warm caches (permissions)
        with django_assert_max_num_queries(40):
            client.get(url("versions", published["template"].pk))

    def test_an_archived_checklist_explains_itself(self, client, published):
        services.archive_checklist_template(published["template"], actor=published["author"])
        client.force_login(published["author"])
        html = body(client.get(url("versions", published["template"].pk)))
        assert "archived" in html and url("version_new", published["template"].pk) not in html


@pytest.mark.django_db
class TestDetail:
    def test_a_published_version_shows_its_record_and_integrity(self, client, published):
        client.force_login(published["reader"])
        html = body(client.get(url("version_detail", published["template"].pk, 1)))
        assert "immutable" in html and "Publication record" in html and "Integrity verified" in html and "sha256:v1:" in html
        assert "Available for new assignments" in html and "Yes - the current version" in html

    def test_a_superseded_version_says_so(self, client, published):
        evolve(published)
        client.force_login(published["reader"])
        html = body(client.get(url("version_detail", published["template"].pk, 1)))
        assert "superseded" in html and "No - superseded by a newer version" in html
        assert url("compare", published["template"].pk) in body(client.get(url("version_detail", published["template"].pk, 2)))

    def test_tampering_is_surfaced(self, client, published):
        from apps.checklists import guards
        from apps.checklists.models import ChecklistItem

        with guards.allow_definition_changes():
            ChecklistItem.objects.filter(section__version=published["v1"], code="ISSUE").update(question="Quietly changed")
        client.force_login(published["reader"])
        html = body(client.get(url("version_detail", published["template"].pk, 1)))
        assert "Integrity mismatch" in html and "no longer matches" in html

    def test_create_a_draft_from_a_historical_version(self, client, published):
        evolve(published)
        client.force_login(published["editor"])
        detail = body(client.get(url("version_detail", published["template"].pk, 1)))
        assert url("version_draft_from", published["template"].pk, 1) in detail
        response = client.post(url("version_draft_from", published["template"].pk, 1))
        assert response.status_code == 302 and response["Location"] == url("builder", published["template"].pk)
        draft = published["template"].versions.get(status="draft")
        assert draft.version_number == 3 and draft.based_on.version_number == 1
        assert item_of(draft, "FRIDGE-TEMP").config["max"] == 10  # the old definition, as a fresh draft

    def test_no_edit_controls_on_a_frozen_version(self, client, published):
        client.force_login(published["author"])
        html = body(client.get(url("version_detail", published["template"].pk, 1)))
        assert "/builder/items/" not in html and "/builder/sections/" not in html and "Open builder" not in html


@pytest.mark.django_db
class TestBuilderControls:
    def test_the_draft_header_offers_the_lifecycle_actions(self, client, world):
        client.force_login(world["author"])
        html = body(client.get(url("builder", world["template"].pk)))
        t, n = world["template"].pk, 1
        for needle in ("Draft", "Preview", "Version history", "Validate", "Publish version", url("version_publish", t, n), url("version_validate", t, n)):
            assert needle in html
        assert 'name="expected_hash"' in html and "Ready to publish" in html and "Editing" in html and "last updated" in html

    def test_discard_is_not_offered_for_the_only_version(self, client, world):
        client.force_login(world["author"])
        assert "Discard draft" not in body(client.get(url("builder", world["template"].pk)))

    def test_discard_is_offered_once_there_is_a_published_version(self, client, published):
        lifecycle.create_draft(published["template"], actor=published["author"])
        client.force_login(published["author"])
        html = body(client.get(url("builder", published["template"].pk)))
        assert "Discard draft" in html and "will be marked superseded" in html

    def test_an_editor_without_publish_rights_has_no_publish_button(self, client, world):
        client.force_login(world["editor"])
        html = body(client.get(url("builder", world["template"].pk)))
        assert "Publish version" not in html and "Validate" in html

    def test_errors_block_the_publish_button_and_are_listed(self, client, org_a, world):
        template = services.create_checklist_template(organization=org_a, name="Blank", code="BLANK-1", actor=world["author"])
        client.force_login(world["author"])
        html = body(client.get(url("builder", template.pk)))
        assert "cannot be published yet" in html and "Add at least one section." in html and "No checklist instructions" in html


@pytest.mark.django_db
class TestActions:
    def test_validate_over_htmx_returns_the_report_and_logs(self, client, world):
        client.force_login(world["author"])
        response = client.post(url("version_validate", world["template"].pk, 1), **HX)
        html = body(response)
        assert response.status_code == 200 and "<html" not in html and 'id="validation-report"' in html and "Ready to publish" in html
        assert ActivityLog.objects.filter(verb="checklist_version.validated").exists()

    def test_validate_without_htmx_flashes_and_redirects(self, client, org_a, world):
        template = services.create_checklist_template(organization=org_a, name="Blank", code="BLANK-2", actor=world["author"])
        client.force_login(world["author"])
        response = client.post(url("version_validate", template.pk, 1), follow=True)
        assert "Add at least one section." in body(response)

    def test_publish_succeeds_and_records_who_and_when(self, client, world):
        client.force_login(world["author"])
        report = lifecycle.run_validation(world["draft"])
        response = client.post(url("version_publish", world["template"].pk, 1), {"change_summary": "First release", "expected_hash": report.draft_hash}, follow=True)
        assert "Version 1 published" in body(response)
        version = ChecklistVersion.objects.get(pk=world["draft"].pk)
        assert version.status == "published" and version.published_by_id == world["author"].pk and version.change_summary == "First release"

    def test_a_blocked_publish_keeps_the_draft_and_lists_the_problems(self, client, org_a, world):
        template = services.create_checklist_template(organization=org_a, name="Blank", code="BLANK-3", actor=world["author"])
        client.force_login(world["author"])
        response = client.post(url("version_publish", template.pk, 1), follow=True)
        text = body(response)
        assert "cannot be published" in text and "Add at least one section." in text
        assert template.versions.get().status == "draft"

    def test_a_stale_publish_is_a_message_not_a_publication(self, client, world):
        client.force_login(world["author"])
        token = lifecycle.run_validation(world["draft"]).draft_hash
        services.update_item(item_of(world["draft"], "FIRE-EXIT"), actor=world["author"], question="Edited after the dialog opened")
        response = client.post(url("version_publish", world["template"].pk, 1), {"expected_hash": token}, follow=True)
        assert "changed after it was last validated" in body(response)
        assert ChecklistVersion.objects.get(pk=world["draft"].pk).status == "draft"

    def test_a_double_submit_is_informational_and_publishes_once(self, client, world):
        client.force_login(world["author"])
        first = client.post(url("version_publish", world["template"].pk, 1))
        second = client.post(url("version_publish", world["template"].pk, 1), follow=True)
        assert first.status_code == 302 and "already published" in body(second)
        assert ActivityLog.objects.filter(verb="checklist_version.published", metadata__template_id=str(world["template"].pk)).count() == 1

    def test_discard_a_draft(self, client, published):
        v2 = lifecycle.create_draft(published["template"], actor=published["author"])
        client.force_login(published["author"])
        response = client.post(url("version_discard", published["template"].pk, 2), follow=True)
        assert "discarded" in body(response) and not ChecklistVersion.objects.filter(pk=v2.pk).exists()
        assert client.post(url("version_discard", published["template"].pk, 1)).status_code == 302  # refused with a message
        assert ChecklistVersion.objects.filter(pk=published["v1"].pk).exists()

    def test_a_second_draft_is_refused_with_a_message(self, client, published):
        lifecycle.create_draft(published["template"], actor=published["author"])
        client.force_login(published["author"])
        response = client.post(url("version_draft_from", published["template"].pk, 1), follow=True)
        assert "already has a draft" in body(response)

    def test_unknown_versions_are_404(self, client, published):
        client.force_login(published["author"])
        for name in ("version_detail", "version_preview"):
            assert client.get(url(name, published["template"].pk, 99)).status_code == 404
        for name in ("version_publish", "version_validate", "version_discard", "version_draft_from"):
            assert client.post(url(name, published["template"].pk, 99)).status_code == 404


@pytest.mark.django_db
class TestAuthorization:
    def test_a_reader_reads_but_every_post_is_forbidden(self, client, published):
        v2 = lifecycle.create_draft(published["template"], actor=published["author"])
        client.force_login(published["reader"])
        t = published["template"].pk
        for name, args in (("versions", ()), ("version_detail", (1,)), ("version_preview", (1,)), ("version_preview", (2,)), ("compare", ())):
            assert client.get(url(name, t, *args)).status_code == 200, name
        for name in ("version_publish", "version_validate", "version_discard", "version_draft_from"):
            assert client.post(url(name, t, 2 if name != "version_draft_from" else 1)).status_code == 403, name
        assert client.post(url("version_new", t)).status_code == 403
        assert client.get(url("builder", t)).status_code == 403
        assert ChecklistVersion.objects.get(pk=v2.pk).status == "draft"

    def test_a_publisher_can_publish_but_not_discard(self, client, published):
        v2 = lifecycle.create_draft(published["template"], actor=published["author"])
        client.force_login(published["publisher"])
        t = published["template"].pk
        assert client.post(url("version_discard", t, 2)).status_code == 403
        assert client.post(url("version_validate", t, 2), **HX).status_code == 200
        assert client.post(url("version_publish", t, 2)).status_code == 302
        assert ChecklistVersion.objects.get(pk=v2.pk).status == "published"

    def test_a_user_without_permissions_is_forbidden(self, client, published):
        client.force_login(published["plain"])
        t = published["template"].pk
        for name, args in (("versions", ()), ("compare", ()), ("version_preview", (1,)), ("version_detail", (1,))):
            assert client.get(url(name, t, *args)).status_code == 403

    def test_another_organization_gets_404_for_everything(self, client, published):
        client.force_login(published["other"])
        t = published["template"].pk
        for name, args in (("versions", ()), ("compare", ()), ("version_preview", (1,)), ("version_detail", (1,))):
            assert client.get(url(name, t, *args)).status_code == 404, name
        for name in ("version_publish", "version_validate", "version_discard", "version_draft_from", "version_new"):
            assert client.post(url(name, t, 1) if name != "version_new" else url(name, t)).status_code == 404, name

    def test_csrf_is_enforced_on_every_lifecycle_post(self, published):
        from django.test import Client

        strict = Client(enforce_csrf_checks=True)
        strict.force_login(published["author"])
        lifecycle.create_draft(published["template"], actor=published["author"])
        for name in ("version_publish", "version_validate", "version_discard"):
            assert strict.post(url(name, published["template"].pk, 2)).status_code == 403
        assert ChecklistVersion.objects.get(template=published["template"], version_number=2).status == "draft"


@pytest.mark.django_db
class TestCompareAndPreview:
    def test_the_compare_page_describes_the_release(self, client, published):
        evolve(published)
        client.force_login(published["reader"])
        html = body(client.get(url("compare", published["template"].pk)))
        assert "v1 → v2" in html and "2 changes" in html and "10 → 8" in html and "Is every fire exit clear?" in html
        assert "Changes behaviour" in html and "Wording" in html and "modified" in html.lower()
        assert ActivityLog.objects.filter(verb="checklist_version.compared").exists()

    def test_filters_narrow_the_list(self, client, published):
        evolve(published)
        client.force_login(published["reader"])
        html = body(client.get(url("compare", published["template"].pk), {"from": 1, "to": 2, "impact": "behavior"}))
        assert "10 → 8" in html and "Is every fire exit clear?" not in html.split("Details")[0].split("Questions")[-1][:0] + html and "setting" in html
        none = body(client.get(url("compare", published["template"].pk), {"from": 1, "to": 2, "category": "rule"}))
        assert "Nothing matches these filters" in none

    def test_identical_versions(self, client, published):
        client.force_login(published["reader"])
        assert "These versions are identical" in body(client.get(url("compare", published["template"].pk), {"from": 1, "to": 1}))

    def test_bad_parameters(self, client, published):
        client.force_login(published["reader"])
        t = published["template"].pk
        assert client.get(url("compare", t), {"from": "x", "to": 1}).status_code == 404
        assert client.get(url("compare", t), {"from": 1, "to": 99}).status_code == 404
        assert client.get(url("compare", t), {"from": 1, "to": 1, "kind": "bogus"}).status_code == 200  # unknown filters are ignored

    def test_user_text_is_escaped_in_compare_and_preview(self, client, published):
        author = published["author"]
        v2 = lifecycle.create_draft(published["template"], actor=author)
        services.update_item(item_of(v2, "FIRE-EXIT"), actor=author, question="<script>alert(1)</script>", help_text="<img src=x onerror=alert(2)>")
        client.force_login(author)
        for page in (client.get(url("compare", published["template"].pk), {"from": 1, "to": 2}), client.get(url("version_preview", published["template"].pk, 2))):
            html = body(page)
            assert "<script>alert(1)" not in html and "<img src=x" not in html and "&lt;script&gt;" in html

    def test_the_preview_renders_every_part_of_the_definition(self, client, published):
        client.force_login(published["reader"])
        html = body(client.get(url("version_preview", published["template"].pk, 1)))
        assert "not an execution" in html and "Fire Safety" in html and "Is the fire exit accessible?" in html and "FIRE-EXIT" in html
        assert "Hidden until a rule shows it" in html  # ISSUE is revealed by a rule
        assert "Rules that affect this question" in html and "R-001: If" in html
        assert "Walk the restaurant and answer every question." in html and "Required" in html and "Optional" in html
        assert "°C" in html and "Good" in html and 'inert' in html

    def test_the_preview_persists_nothing(self, client, published):
        client.force_login(published["author"])
        before = (ChecklistVersion.objects.count(), ActivityLog.objects.count())
        client.get(url("version_preview", published["template"].pk, 1))
        assert (ChecklistVersion.objects.count(), ActivityLog.objects.count()) == before

    def test_a_draft_can_be_previewed(self, client, world):
        client.force_login(world["author"])
        assert "Preview" in body(client.get(url("version_preview", world["template"].pk, 1)))
