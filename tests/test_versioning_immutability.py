"""A published (or superseded) definition cannot be silently changed - through any supported path, and not even by raw SQL.

Layers under test: services, models, API, web, admin, import, and the PostgreSQL triggers (``guards.py``) that refuse
``QuerySet.update()``, bulk operations, cascaded deletes and hand-written SQL.  Maintenance (sealing legacy versions) is the one
explicit, transaction-scoped override.
"""

from __future__ import annotations

import io

import pytest
from django.contrib import admin
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.db import IntegrityError, connection, transaction
from django.test import RequestFactory
from django.urls import reverse
from rest_framework.test import APIClient

from apps.checklists import guards, imports, lifecycle, services
from apps.checklists.manifest import manifest_hash, verify_version_integrity
from apps.checklists.models import (
    ChecklistItem,
    ChecklistOption,
    ChecklistRule,
    ChecklistSection,
    ChecklistTemplate,
    ChecklistVersion,
)

from versioning_support import act, build_world, item_of


@pytest.fixture
def world(org_a, org_b, make_user, assign):
    return build_world(org_a, org_b, make_user, assign, publish=True)


@pytest.fixture
def superseded(world):
    """v1 is superseded, v2 is published (both frozen)."""
    v2 = lifecycle.create_draft(world["template"], actor=world["author"])
    lifecycle.publish_version(v2, actor=world["author"])
    world["v1"].refresh_from_db()
    world["v2"] = v2
    return world


def blocked(call):
    """The call must be refused by the database (constraint ``checklist_*_immutable`` / ``checklist_version_*``)."""
    with pytest.raises(IntegrityError) as info, transaction.atomic():
        call()
    assert "checklist" in str(info.value).lower()


# ---------------------------------------------------------------------------
@pytest.mark.django_db
class TestModelLayer:
    def test_children_of_a_published_version_cannot_be_saved_or_deleted(self, world):
        v1 = world["v1"]
        for obj in (v1.sections.first(), item_of(v1, "FRIDGE-TEMP"), item_of(v1, "CONDITION").options.first(), v1.rules.get()):
            obj.refresh_from_db()
            with pytest.raises(ValidationError) as info:
                obj.save()
            assert info.value.code == "version_immutable"
            with pytest.raises(ValidationError) as info:
                obj.delete()
            assert info.value.code == "version_immutable"

    def test_new_children_cannot_be_attached_to_a_published_version(self, world):
        section = world["v1"].sections.first()
        with pytest.raises(ValidationError):
            ChecklistItem(section=section, code="NEW", question="Q", sort_order=99).save()
        with pytest.raises(ValidationError):
            ChecklistSection(version=world["v1"], code="S-9", name="N", sort_order=9).save()
        with pytest.raises(ValidationError):
            ChecklistRule(version=world["v1"], code="R-9", name="N", actions=[], sort_order=9).save()

    def test_the_version_itself_is_frozen(self, world):
        v1 = ChecklistVersion.objects.get(pk=world["v1"].pk)
        v1.change_summary = "rewritten history"
        with pytest.raises(ValidationError):
            v1.save()
        v1.refresh_from_db()
        v1.status = "draft"
        v1.published_at = None
        with pytest.raises(ValidationError):
            v1.save()
        with pytest.raises(ValidationError):
            v1.delete()

    def test_a_superseded_version_is_just_as_frozen(self, superseded):
        v1 = ChecklistVersion.objects.get(pk=superseded["v1"].pk)
        assert v1.status == "superseded"
        for call in (lambda: v1.save(), lambda: v1.delete(), lambda: item_of(v1, "FIRE-EXIT").save(), lambda: v1.rules.get().delete()):
            with pytest.raises(ValidationError):
                call()
        with pytest.raises(ValidationError):
            services.update_item(item_of(v1, "FIRE-EXIT"), actor=superseded["author"], question="X")

    def test_drafts_remain_editable_at_every_layer(self, world):
        v2 = lifecycle.create_draft(world["template"], actor=world["author"])
        item = item_of(v2, "FIRE-EXIT")
        item.question = "Changed in the draft"
        item.save()
        ChecklistItem.objects.filter(pk=item.pk).update(help_text="bulk edit of a draft is fine")
        ChecklistOption.objects.filter(item__section__version=v2, value="poor").delete()
        assert ChecklistItem.objects.get(pk=item.pk).help_text.startswith("bulk")


# ---------------------------------------------------------------------------
@pytest.mark.django_db
class TestDatabaseLayer:
    """The model guards cannot see these paths; PostgreSQL refuses them."""

    def test_queryset_update_and_delete_of_children(self, world):
        v1 = world["v1"]
        blocked(lambda: ChecklistItem.objects.filter(section__version=v1).update(question="rewritten"))
        blocked(lambda: ChecklistSection.objects.filter(version=v1).update(name="rewritten"))
        blocked(lambda: ChecklistOption.objects.filter(item__section__version=v1).update(label="rewritten"))
        blocked(lambda: ChecklistRule.objects.filter(version=v1).update(actions=[]))
        blocked(lambda: ChecklistItem.objects.filter(section__version=v1).delete())
        blocked(lambda: ChecklistOption.objects.filter(item__section__version=v1).delete())
        blocked(lambda: ChecklistRule.objects.filter(version=v1).delete())
        assert item_of(v1, "FIRE-EXIT").question == "Is the fire exit accessible?" and v1.rules.count() == 1

    def test_bulk_create_and_bulk_update_on_a_published_version(self, world):
        v1 = world["v1"]
        section = v1.sections.first()
        blocked(lambda: ChecklistItem.objects.bulk_create([ChecklistItem(section=section, code="BULK", question="Q", sort_order=77)]))
        items = list(ChecklistItem.objects.filter(section__version=v1))
        for item in items:
            item.question = "x"
        blocked(lambda: ChecklistItem.objects.bulk_update(items, ["question"]))

    def test_moving_a_row_into_or_out_of_a_published_version(self, world):
        v1 = world["v1"]
        v2 = lifecycle.create_draft(world["template"], actor=world["author"])
        draft_item = item_of(v2, "ISSUE")
        blocked(lambda: ChecklistItem.objects.filter(pk=draft_item.pk).update(section=v1.sections.first()))  # draft -> published
        blocked(lambda: ChecklistItem.objects.filter(pk=item_of(v1, "ISSUE").pk).update(section=v2.sections.first()))  # published -> draft

    def test_deleting_a_version_cascades_nothing(self, world):
        blocked(lambda: ChecklistVersion.objects.filter(pk=world["v1"].pk).delete())
        assert world["v1"].sections.count() == 3

    def test_the_publication_record_cannot_be_rewritten(self, world):
        v1 = world["v1"]
        for field, value in (("change_summary", "rewritten"), ("integrity_hash", "sha256:v1:" + "0" * 64), ("publication_report", {"x": 1}),
                             ("published_at", v1.published_at.replace(year=2001)), ("version_number", 9)):
            blocked(lambda f=field, v=value: ChecklistVersion.objects.filter(pk=v1.pk).update(**{f: v}))

    def test_illegal_transitions_are_refused_by_the_database(self, superseded):
        v1, v2 = superseded["v1"], superseded["v2"]
        blocked(lambda: ChecklistVersion.objects.filter(pk=v2.pk).update(status="draft", published_at=None))  # published -> draft
        blocked(lambda: ChecklistVersion.objects.filter(pk=v1.pk).update(status="published", superseded_at=None))  # superseded -> published
        blocked(lambda: ChecklistVersion.objects.filter(pk=v1.pk).update(status="draft", published_at=None, superseded_at=None))
        assert ChecklistVersion.objects.get(pk=v2.pk).status == "published"

    def test_raw_sql_is_refused_too(self, world):
        v1 = world["v1"]
        for sql in ("UPDATE checklists_checklistitem SET question = 'raw' WHERE code = 'FIRE-EXIT'",
                    "DELETE FROM checklists_checklistoption",
                    "UPDATE checklists_checklistversion SET change_summary = 'raw'"):
            with pytest.raises(Exception) as info, transaction.atomic(), connection.cursor() as cursor:
                cursor.execute(sql)
            assert "checklist" in str(info.value).lower()
        assert item_of(v1, "FIRE-EXIT").question == "Is the fire exit accessible?"

    def test_the_lifecycle_transitions_themselves_still_work(self, world):
        v2 = lifecycle.create_draft(world["template"], actor=world["author"])  # inserts a draft + children
        lifecycle.publish_version(v2, actor=world["author"])  # draft -> published, published -> superseded
        lifecycle.discard_draft(lifecycle.create_draft(world["template"], actor=world["author"]), actor=world["author"])  # cascade delete of a draft
        assert {v.version_number: v.status for v in world["template"].versions.all()} == {1: "superseded", 2: "published"}

    def test_history_tables_are_not_guarded(self, world):
        v1 = world["v1"]
        assert v1.history.count() >= 2  # the published row's history stays writable/readable (append-only by design)

    def test_the_triggers_are_installed_idempotently(self):
        assert guards.install_database_guards() is True and guards.install_database_guards() is True
        with connection.cursor() as cursor:
            cursor.execute("SELECT count(*) FROM pg_trigger WHERE tgname IN ('checklists_definition_guard', 'checklists_version_guard')")
            assert cursor.fetchone()[0] == 5


@pytest.mark.django_db
class TestMaintenanceOverride:
    def test_the_override_is_explicit_scoped_and_temporary(self, world):
        v1 = world["v1"]
        with guards.allow_definition_changes():
            ChecklistItem.objects.filter(section__version=v1, code="ISSUE").update(question="tampered")  # a deliberate, audited act
            item = item_of(v1, "ISSUE")
            item.question = "tampered again"
            item.save()
        assert item_of(v1, "ISSUE").question == "tampered again"
        blocked(lambda: ChecklistItem.objects.filter(section__version=v1).update(question="after"))  # blocked again outside the block
        with pytest.raises(ValidationError):
            item_of(v1, "ISSUE").save()

    def test_tampering_is_detected_by_the_integrity_hash(self, world):
        v1 = ChecklistVersion.objects.get(pk=world["v1"].pk)
        assert verify_version_integrity(v1).ok
        with guards.allow_definition_changes():
            ChecklistItem.objects.filter(section__version=v1, code="FRIDGE-TEMP").update(config={"min": 0, "max": 99, "decimals": 1, "unit": "°C"})
        result = verify_version_integrity(v1)
        assert result.status == "mismatch" and result.expected == v1.integrity_hash != result.actual
        out = io.StringIO()
        with pytest.raises(Exception) as info:
            call_command("verify_checklist_integrity", stdout=out, stderr=io.StringIO())
        assert "failed verification" in str(info.value)

    def test_legacy_versions_without_a_hash_are_sealed_by_the_command(self, world):
        v1 = world["v1"]
        with guards.allow_definition_changes():
            ChecklistVersion.objects.filter(pk=v1.pk).update(integrity_hash="")  # simulate a version published before hashes existed
        assert verify_version_integrity(ChecklistVersion.objects.get(pk=v1.pk)).status == "unsealed"
        out = io.StringIO()
        call_command("seal_checklist_versions", stdout=out)
        sealed = ChecklistVersion.objects.get(pk=v1.pk)
        assert "Sealed 1 version" in out.getvalue() and verify_version_integrity(sealed).ok
        call_command("seal_checklist_versions", stdout=out)  # idempotent
        assert ChecklistVersion.objects.get(pk=v1.pk).integrity_hash == sealed.integrity_hash
        from apps.core.models import ActivityLog

        assert ActivityLog.objects.filter(verb="checklist_version.sealed").count() == 1


# ---------------------------------------------------------------------------
@pytest.mark.django_db
class TestTemplateMetadataStaysEditable:
    def test_descriptive_template_changes_do_not_touch_the_definition(self, world):
        before = manifest_hash(world["v1"])
        services.update_checklist_template(world["template"], actor=world["author"], name="Renamed checklist", instructions="New general guidance")
        assert manifest_hash(world["v1"]) == before and verify_version_integrity(ChecklistVersion.objects.get(pk=world["v1"].pk)).ok


# ---------------------------------------------------------------------------
@pytest.mark.django_db
class TestWebAndApiPaths:
    """Direct URLs and requests against a template whose versions are all frozen."""

    def test_builder_endpoints_refuse_a_template_with_no_draft(self, client, world):
        client.force_login(world["author"])
        tid = world["template"].pk
        section, item = world["v1"].sections.first(), item_of(world["v1"], "ISSUE")
        before = item.question
        attempts = [
            ("post", reverse("checklists:section_new", args=[tid]), {"name": "X"}),
            ("post", reverse("checklists:section_edit", args=[tid, section.pk]), {"name": "X"}),
            ("post", reverse("checklists:section_delete", args=[tid, section.pk]), {}),
            ("post", reverse("checklists:item_new", args=[tid, section.pk]), {"question": "X", "answer_type": "yes_no"}),
            ("post", reverse("checklists:item_edit", args=[tid, item.pk]), {"question": "X", "answer_type": "long_text"}),
            ("post", reverse("checklists:item_delete", args=[tid, item.pk]), {}),
            ("post", reverse("checklists:item_move", args=[tid, item.pk]), {"direction": "up"}),
            ("post", reverse("checklists:rule_new", args=[tid]), {"name": "X"}),
            ("post", reverse("checklists:rule_delete", args=[tid, world["v1"].rules.get().pk]), {}),
        ]
        for method, url, data in attempts:
            assert getattr(client, method)(url, data).status_code == 404, url
        assert item_of(world["v1"], "ISSUE").question == before and world["v1"].rules.count() == 1 and world["v1"].sections.count() == 3

    def test_the_detail_page_offers_no_edit_controls(self, client, world):
        client.force_login(world["author"])
        html = client.get(reverse("checklists:version_detail", args=[world["template"].pk, 1])).content.decode()
        assert "immutable" in html and "/builder/items/" not in html and "/builder/sections/" not in html

    def test_api_item_and_rule_writes_are_refused(self, world):
        client = APIClient()
        client.force_authenticate(world["author"])
        tid = world["template"].pk
        item = item_of(world["v1"], "ISSUE")
        section = world["v1"].sections.first()
        assert client.post(f"/api/v1/checklists/{tid}/items/", {"section": str(section.pk), "question": "X"}, format="json").status_code == 400
        assert client.patch(f"/api/v1/checklists/{tid}/items/{item.pk}/", {"question": "X"}, format="json").status_code == 400
        assert client.delete(f"/api/v1/checklists/{tid}/items/{item.pk}/").status_code == 400
        rule = world["v1"].rules.get()
        assert client.post(f"/api/v1/checklists/{tid}/rules/", {"name": "X", "actions": [act("DISPLAY_MESSAGE", message="x")]}, format="json").status_code == 400
        assert client.patch(f"/api/v1/checklists/{tid}/rules/{rule.pk}/", {"name": "X"}, format="json").status_code == 400
        assert client.delete(f"/api/v1/checklists/{tid}/rules/{rule.pk}/").status_code == 400
        assert client.delete(f"/api/v1/checklist-versions/{world['v1'].pk}/").status_code == 400
        assert item_of(world["v1"], "ISSUE").question == "Describe any identified issue." and world["v1"].rules.count() == 1


# ---------------------------------------------------------------------------
@pytest.mark.django_db
class TestAdminAndImport:
    def request(self, user):
        request = RequestFactory().get("/")
        request.user = user
        return request

    def test_every_definition_admin_is_read_only_for_a_published_version(self, world, superuser):
        request = self.request(superuser)
        v1 = world["v1"]
        objs = {ChecklistSection: v1.sections.first(), ChecklistItem: item_of(v1, "FIRE-EXIT"), ChecklistOption: item_of(v1, "CONDITION").options.first(),
                ChecklistRule: v1.rules.get(), ChecklistVersion: ChecklistVersion.objects.get(pk=v1.pk)}
        for model, obj in objs.items():
            model_admin = admin.site._registry[model]
            assert not model_admin.has_change_permission(request, obj), model
            assert not model_admin.has_delete_permission(request, obj), model
        version_admin = admin.site._registry[ChecklistVersion]
        assert not version_admin.has_add_permission(request) and not version_admin.has_delete_permission(request)
        assert {"status", "integrity_hash", "published_by", "superseded_at"} <= set(version_admin.get_readonly_fields(request, objs[ChecklistVersion]))

    def test_inlines_are_frozen_too(self, world, superuser):
        request = self.request(superuser)
        section_admin = admin.site._registry[ChecklistSection]
        item_admin = admin.site._registry[ChecklistItem]
        for model_admin, parent in ((section_admin, world["v1"].sections.first()), (item_admin, item_of(world["v1"], "CONDITION"))):
            for inline_class in model_admin.inlines:
                inline = inline_class(model_admin.model, admin.site)
                assert not inline.has_add_permission(request, parent) and not inline.has_change_permission(request, parent)
                assert not inline.has_delete_permission(request, parent)

    def test_the_admin_change_form_refuses_a_published_row(self, client, world, superuser):
        client.force_login(superuser)
        item = item_of(world["v1"], "FIRE-EXIT")
        url = reverse("admin:checklists_checklistitem_change", args=[item.pk])
        response = client.post(url, {"question": "Rewritten", "code": item.code, "answer_type": "yes_no", "sort_order": item.sort_order, "section": item.section_id})
        assert response.status_code in (302, 403) and item_of(world["v1"], "FIRE-EXIT").question == "Is the fire exit accessible?"

    def test_drafts_remain_editable_in_the_admin(self, world, superuser):
        v2 = lifecycle.create_draft(world["template"], actor=world["author"])
        request = self.request(superuser)
        assert admin.site._registry[ChecklistItem].has_change_permission(request, item_of(v2, "FIRE-EXIT"))

    def test_imports_cannot_reach_a_published_version(self, world, org_a):
        rows = [{"template": "RDSC", "section": "Fire Safety", "question": "Sneaked in", "code": "SNEAK"}]
        with pytest.raises(ValidationError) as info:
            imports.import_items(user=world["author"], organization=org_a, rows=rows)
        assert "not found" in " ".join(info.value.messages)
        rule_rows = [{"template": "RDSC", "name": "x", "actions": '[{"type": "DISPLAY_MESSAGE", "message": "x"}]'}]
        with pytest.raises(ValidationError):
            imports.import_rules(user=world["author"], organization=org_a, rows=rule_rows)
        assert not ChecklistItem.objects.filter(code="SNEAK").exists() and world["v1"].rules.count() == 1

    def test_an_import_goes_into_the_draft_only(self, world, org_a):
        v2 = lifecycle.create_draft(world["template"], actor=world["author"])
        rows = [{"template": "RDSC", "section": "Fire Safety", "question": "New in the draft", "code": "NEWQ"}]
        imports.import_items(user=world["author"], organization=org_a, rows=rows)
        assert ChecklistItem.objects.filter(section__version=v2, code="NEWQ").exists()
        assert not ChecklistItem.objects.filter(section__version=world["v1"], code="NEWQ").exists()
