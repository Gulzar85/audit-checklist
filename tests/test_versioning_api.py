"""The version API: lifecycle actions, comparison, integrity - authorization, isolation and error contracts."""

from __future__ import annotations

import pytest
from rest_framework.test import APIClient

from apps.checklists import lifecycle, services
from apps.checklists.models import ChecklistVersion
from apps.core.models import ActivityLog

from versioning_support import build_world, item_of


def api(user=None) -> APIClient:
    client = APIClient()
    if user is not None:
        client.force_authenticate(user)
    return client


def data(response):
    payload = response.json()
    return payload.get("data", payload) if isinstance(payload, dict) else payload


def error(response):
    return response.json()["error"]


@pytest.fixture
def world(org_a, org_b, make_user, assign):
    return build_world(org_a, org_b, make_user, assign)


@pytest.fixture
def published(org_a, org_b, make_user, assign):
    return build_world(org_a, org_b, make_user, assign, publish=True)


def vurl(version, tail=""):
    return f"/api/v1/checklist-versions/{version.pk}/{tail}"


@pytest.mark.django_db
class TestHistoryAndDetail:
    def test_the_history_lists_versions_with_their_lifecycle_record(self, published):
        v2 = lifecycle.create_draft(published["template"], actor=published["author"], change_summary="Next")
        rows = data(api(published["reader"]).get(f"/api/v1/checklists/{published['template'].pk}/versions/"))
        assert [r["version_number"] for r in rows] == [2, 1]
        draft, first = rows
        assert (draft["status"], draft["based_on"], draft["published_at"], draft["integrity_hash"]) == ("draft", 1, None, "")
        assert first["status"] == "published" and first["published_by"] == str(published["author"].pk) and first["integrity_hash"].startswith("sha256:v1:")
        assert first["section_count"] == 3 and first["item_count"] == 5 and first["superseded_at"] is None
        assert v2.pk and first["based_on"] is None

    def test_detail_with_integrity_and_assignability(self, published):
        detail = data(api(published["reader"]).get(vurl(published["v1"])))
        assert detail["status"] == "published" and detail["integrity"] == {"status": "verified", "verified": True} and detail["assignable"] is True
        assert detail["publication_warnings"] == [] or isinstance(detail["publication_warnings"], list)

    def test_the_history_query_count_is_flat(self, published, django_assert_max_num_queries):
        author = published["author"]
        for _ in range(5):
            lifecycle.publish_version(lifecycle.create_draft(published["template"], actor=author), actor=author)
        with django_assert_max_num_queries(12):
            assert len(data(api(author).get(f"/api/v1/checklists/{published['template'].pk}/versions/"))) == 6

    def test_unauthenticated_and_foreign_requests(self, published):
        assert api().get(vurl(published["v1"])).status_code in (401, 403)
        foreign = api(published["other"])
        assert foreign.get(vurl(published["v1"])).status_code == 404
        assert foreign.get(f"/api/v1/checklists/{published['template'].pk}/versions/").status_code == 404
        assert api(published["author"]).get(vurl(published["foreign_version"])).status_code == 404  # org B's version: never revealed
        assert api(published["author"]).get("/api/v1/checklist-versions/not-a-uuid/").status_code == 404


@pytest.mark.django_db
class TestValidateAndPublish:
    def test_validate_returns_the_publication_report(self, world):
        result = data(api(world["author"]).post(vurl(world["draft"], "validate/")))
        assert result["valid"] is True and result["errors"] == [] and result["draft_hash"].startswith("sha256:v1:") and result["stats"]["items"] == 5
        assert ActivityLog.objects.filter(verb="checklist_version.validated").exists()

    def test_validate_reports_blocking_errors_as_data(self, org_a, make_user, assign):
        author = make_user(email="api-v@example.com", organization=org_a, role=None)
        assign(author, org_a, "administrator")
        template = services.create_checklist_template(organization=org_a, name="Empty", code="EMP-API", actor=author)
        response = api(author).post(vurl(template.versions.get(), "validate/"))
        assert response.status_code == 200 and data(response)["valid"] is False and data(response)["errors"][0]["code"] == "no_sections"

    def test_publish_succeeds_and_supersedes(self, published):
        author = published["author"]
        v2 = lifecycle.create_draft(published["template"], actor=author)
        token = data(api(author).post(vurl(v2, "validate/")))["draft_hash"]
        response = api(author).post(vurl(v2, "publish/"), {"change_summary": "Second release", "expected_hash": token}, format="json")
        assert response.status_code == 200
        body = data(response)
        assert body["status"] == "published" and body["change_summary"] == "Second release" and body["integrity_hash"] == token
        assert data(api(author).get(vurl(published["v1"])))["status"] == "superseded"
        assert data(api(author).get(vurl(published["v1"])))["assignable"] is False

    def test_publish_blocked_returns_400_with_the_structured_report(self, org_a, make_user, assign):
        author = make_user(email="api-p@example.com", organization=org_a, role=None)
        assign(author, org_a, "administrator")
        template = services.create_checklist_template(organization=org_a, name="Empty", code="EMP-API2", actor=author)
        response = api(author).post(vurl(template.versions.get(), "publish/"), {}, format="json")
        assert response.status_code == 400 and error(response)["code"] == "VALIDATION_ERROR"
        report = error(response)["details"]["report"]
        assert report["valid"] is False and report["errors"][0]["code"] == "no_sections"
        assert ChecklistVersion.objects.get(pk=template.versions.get().pk).status == "draft"

    def test_a_stale_draft_is_a_409_conflict(self, world):
        author = world["author"]
        token = data(api(author).post(vurl(world["draft"], "validate/")))["draft_hash"]
        services.update_item(item_of(world["draft"], "FIRE-EXIT"), actor=author, question="Changed after validation")
        response = api(author).post(vurl(world["draft"], "publish/"), {"expected_hash": token}, format="json")
        assert response.status_code == 409 and error(response)["code"] == "CONFLICT" and error(response)["details"]["code"] == "stale_draft"
        assert ChecklistVersion.objects.get(pk=world["draft"].pk).status == "draft"

    def test_a_repeated_publish_is_a_409_and_publishes_nothing_twice(self, world):
        author = world["author"]
        assert api(author).post(vurl(world["draft"], "publish/"), {}, format="json").status_code == 200
        again = api(author).post(vurl(world["draft"], "publish/"), {}, format="json")
        assert again.status_code == 409 and error(again)["details"]["code"] == "already_published"
        assert ActivityLog.objects.filter(verb="checklist_version.published", metadata__template_id=str(world["template"].pk)).count() == 1

    def test_malformed_input_is_a_400(self, world):
        response = api(world["author"]).post(vurl(world["draft"], "publish/"), {"change_summary": "x" * 400}, format="json")
        assert response.status_code == 400


@pytest.mark.django_db
class TestDraftsAndDiscard:
    def test_create_draft_from_a_version(self, published):
        author = published["author"]
        response = api(author).post(vurl(published["v1"], "create-draft/"), {"change_summary": "From v1"}, format="json")
        assert response.status_code == 201 and data(response)["status"] == "draft" and data(response)["based_on"] == 1 and data(response)["version_number"] == 2
        assert api(author).post(vurl(published["v1"], "create-draft/"), {}, format="json").status_code == 400  # a draft already exists

    def test_the_history_endpoint_can_create_a_draft_too(self, published):
        url = f"/api/v1/checklists/{published['template'].pk}/versions/"
        response = api(published["author"]).post(url, {"change_summary": "x"}, format="json")
        assert response.status_code == 201 and data(response)["version_number"] == 2
        assert api(published["author"]).post(url, {"source": str(published["foreign_version"].pk)}, format="json").status_code in (400, 404)

    def test_a_foreign_source_version_is_not_found(self, published):
        lifecycle.discard_draft(lifecycle.create_draft(published["template"], actor=published["author"]), actor=published["author"])
        url = f"/api/v1/checklists/{published['template'].pk}/versions/"
        assert api(published["author"]).post(url, {"source": str(published["foreign_version"].pk)}, format="json").status_code == 404

    def test_discard_a_draft_but_never_a_published_version(self, published):
        author = published["author"]
        v2 = lifecycle.create_draft(published["template"], actor=author)
        assert api(author).delete(vurl(v2)).status_code == 204 and not ChecklistVersion.objects.filter(pk=v2.pk).exists()
        response = api(author).delete(vurl(published["v1"]))
        assert response.status_code == 400 and ChecklistVersion.objects.filter(pk=published["v1"].pk).exists()

    def test_the_only_version_cannot_be_discarded(self, world):
        response = api(world["author"]).delete(vurl(world["draft"]))
        assert response.status_code == 400 and error(response)["details"]["code"] == "only_version"


@pytest.mark.django_db
class TestCompareAndIntegrity:
    def evolve(self, published):
        author = published["author"]
        v2 = lifecycle.create_draft(published["template"], actor=author)
        services.update_item(item_of(v2, "FRIDGE-TEMP"), actor=author, config={"min": 0, "max": 8, "decimals": 1, "unit": "°C"})
        services.update_item(item_of(v2, "FIRE-EXIT"), actor=author, question="Is every fire exit clear?")
        lifecycle.publish_version(v2, actor=author)
        return v2

    def test_compare_by_number_and_by_id(self, published):
        v2 = self.evolve(published)
        base = f"/api/v1/checklists/{published['template'].pk}/versions/compare/"
        by_number = data(api(published["reader"]).get(base, {"from": 1, "to": 2}))
        by_id = data(api(published["reader"]).get(base, {"from": str(published["v1"].pk), "to": str(v2.pk)}))
        assert by_number == by_id and by_number["from"] == 1 and by_number["to"] == 2 and not by_number["identical"]
        assert by_number["counts"]["total"] == 2 and {c["field"] for c in by_number["changes"]} == {"config.max", "question"}

    def test_filters(self, published):
        self.evolve(published)
        base = f"/api/v1/checklists/{published['template'].pk}/versions/compare/"
        behaviour = data(api(published["author"]).get(base, {"from": 1, "to": 2, "impact": "behavior"}))
        assert [c["field"] for c in behaviour["changes"]] == ["config.max"] and behaviour["counts"]["total"] == 2
        assert api(published["author"]).get(base, {"from": 1, "to": 2, "kind": "bogus"}).status_code == 400

    def test_versions_of_another_checklist_or_organization_are_rejected(self, published, org_a):
        other = services.create_checklist_template(organization=org_a, name="Other", code="OTH-API", actor=published["author"])
        base = f"/api/v1/checklists/{published['template'].pk}/versions/compare/"
        assert api(published["author"]).get(base, {"from": 1, "to": str(other.versions.get().pk)}).status_code == 404
        assert api(published["author"]).get(base, {"from": 1, "to": str(published["foreign_version"].pk)}).status_code == 404
        assert api(published["author"]).get(base, {"from": 1}).status_code == 400
        assert api(published["author"]).get(base, {"from": 1, "to": 99}).status_code == 404
        foreign_base = f"/api/v1/checklists/{published['foreign'].pk}/versions/compare/"
        assert api(published["author"]).get(foreign_base, {"from": 1, "to": 1}).status_code == 404

    def test_manifest_and_integrity_endpoints(self, published):
        manifest = data(api(published["reader"]).get(vurl(published["v1"], "manifest/")))
        assert manifest["hash"] == manifest["integrity_hash"] and manifest["manifest"]["schema"] == 1 and len(manifest["manifest"]["sections"]) == 3
        integrity = data(api(published["reader"]).get(vurl(published["v1"], "integrity/")))
        assert integrity["verified"] is True and integrity["status"] == "verified"


@pytest.mark.django_db
class TestAuthorization:
    def test_a_reader_can_read_everything_and_change_nothing(self, published):
        reader, v1 = api(published["reader"]), published["v1"]
        v2 = lifecycle.create_draft(published["template"], actor=published["author"])
        assert reader.get(vurl(v1)).status_code == 200 and reader.get(vurl(v2)).status_code == 200  # history is readable, drafts included
        for method, url, body in (("post", vurl(v2, "validate/"), {}), ("post", vurl(v2, "publish/"), {}), ("post", vurl(v1, "create-draft/"), {}), ("delete", vurl(v2), None)):
            response = getattr(reader, method)(url, body, format="json") if body is not None else getattr(reader, method)(url)
            assert response.status_code == 403, url
        assert ChecklistVersion.objects.get(pk=v2.pk).status == "draft"

    def test_an_editor_validates_and_discards_but_cannot_publish(self, published):
        editor = api(published["editor"])
        v2 = lifecycle.create_draft(published["template"], actor=published["author"])
        assert editor.post(vurl(v2, "validate/")).status_code == 200
        assert editor.post(vurl(v2, "publish/"), {}, format="json").status_code == 403
        assert editor.delete(vurl(v2)).status_code == 204
        assert editor.post(vurl(published["v1"], "create-draft/"), {}, format="json").status_code == 201

    def test_a_publisher_publishes_and_validates_but_cannot_edit_or_create(self, published):
        publisher = api(published["publisher"])
        v2 = lifecycle.create_draft(published["template"], actor=published["author"])
        assert publisher.post(vurl(v2, "validate/")).status_code == 200
        assert publisher.delete(vurl(v2)).status_code == 403
        assert publisher.post(vurl(published["v1"], "create-draft/"), {}, format="json").status_code == 403  # creating drafts needs checklist.update
        assert publisher.post(vurl(v2, "publish/"), {}, format="json").status_code == 200

    def test_a_member_without_checklist_permissions_sees_nothing(self, published):
        plain = api(published["plain"])
        assert plain.get(vurl(published["v1"])).status_code == 404 and plain.post(vurl(published["v1"], "create-draft/")).status_code == 404

    def test_other_organizations_cannot_touch_versions(self, published):
        other = api(published["other"])
        v1 = published["v1"]
        for method, url in (("get", vurl(v1)), ("post", vurl(v1, "validate/")), ("post", vurl(v1, "publish/")), ("post", vurl(v1, "create-draft/")), ("delete", vurl(v1)),
                            ("get", vurl(v1, "manifest/")), ("get", vurl(v1, "integrity/"))):
            assert getattr(other, method)(url).status_code == 404, url

    def test_scope_restricted_roles_do_not_grant_definition_access(self, published, org_a, make_user, assign, make_location):
        """Checklist definitions are organization-level: a role held only at one location opens nothing here."""
        location = make_location(org_a, "Store 1")
        scoped = make_user(email="vs-scoped@example.com", organization=org_a, role=None)
        assign(scoped, org_a, "administrator", scope="location", location=location)
        assert api(scoped).get(vurl(published["v1"])).status_code == 404
        assert api(scoped).post(vurl(published["v1"], "create-draft/"), {}, format="json").status_code == 404

    def test_inactive_users_are_refused(self, published):
        from apps.accounts.constants import UserStatus

        published["author"].status = UserStatus.SUSPENDED  # the lifecycle status is authoritative; is_active mirrors it
        published["author"].save()
        assert api(published["author"]).get(vurl(published["v1"])).status_code in (401, 403)
