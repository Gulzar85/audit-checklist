"""The version lifecycle: draft -> validate -> publish -> superseded, discard, rollback, archival and assignability."""

from __future__ import annotations

import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction

from apps.checklists import constants as c
from apps.checklists import lifecycle, selectors, services
from apps.checklists.models import ChecklistVersion
from apps.core.models import ActivityLog

from versioning_support import build_world, item_of


@pytest.fixture
def world(org_a, org_b, make_user, assign):
    return build_world(org_a, org_b, make_user, assign)


@pytest.fixture
def published(org_a, org_b, make_user, assign):
    return build_world(org_a, org_b, make_user, assign, publish=True)


def verbs():
    return list(ActivityLog.objects.order_by("created_at").values_list("verb", flat=True))


@pytest.mark.django_db
class TestDraftAndPublish:
    def test_a_new_template_starts_with_an_empty_draft(self, org_a):
        template = services.create_checklist_template(organization=org_a, name="Fresh", code="FRESH-1")
        version = template.versions.get()
        assert (version.version_number, version.status, version.based_on_id, version.integrity_hash) == (1, "draft", None, "")
        assert version.published_at is None and version.published_by is None and version.superseded_at is None

    def test_a_draft_can_be_edited_validated_and_published(self, world):
        draft, author = world["draft"], world["author"]
        services.update_item(item_of(draft, "FRIDGE-TEMP"), actor=author, question="Record the refrigerator temperature (°C).")
        report = lifecycle.run_validation(draft, actor=author)
        assert report.ok and report.draft_hash.startswith("sha256:v1:")
        version = lifecycle.publish_version(draft, actor=author, change_summary="  First release  ", expected_hash=report.draft_hash)
        version.refresh_from_db()
        assert version.status == c.VersionStatus.PUBLISHED and version.change_summary == "First release"
        assert version.published_by_id == author.pk and version.published_at and version.superseded_at is None
        assert version.integrity_hash == report.draft_hash
        assert version.publication_report["stats"] == {"sections": 3, "items": 5, "options": 8, "rules": 1}

    def test_an_invalid_draft_cannot_be_published_and_nothing_changes(self, org_a):
        template = services.create_checklist_template(organization=org_a, name="Empty", code="EMPTY-1")
        draft = template.versions.get()
        with pytest.raises(lifecycle.PublicationBlocked) as info:
            lifecycle.publish_version(draft)
        assert "Add at least one section." in info.value.messages and info.value.report.errors
        draft.refresh_from_db()
        assert draft.status == "draft" and draft.published_at is None
        assert "checklist_version.validation_failed" in verbs() and "checklist_version.published" not in verbs()

    def test_a_published_version_is_read_only_through_every_service(self, published):
        v1, author = published["v1"], published["author"]
        section = v1.sections.first()
        item = item_of(v1, "ISSUE")
        rule = v1.rules.get()
        calls = [
            lambda: services.create_section(v1, name="X", actor=author),
            lambda: services.update_section(section, name="X", actor=author),
            lambda: services.delete_section(section, actor=author),
            lambda: services.create_item(section, question="X", actor=author),
            lambda: services.update_item(item, question="X", actor=author),
            lambda: services.delete_item(item, actor=author),
            lambda: services.create_rule(v1, name="X", actions=[{"type": "DISPLAY_MESSAGE", "message": "x"}], actor=author),
            lambda: services.update_rule(rule, name="X", actor=author),
            lambda: services.delete_rule(rule, actor=author),
            lambda: services.reorder_sections(v1, [s.pk for s in v1.sections.all()], actor=author),
        ]
        for call in calls:
            with pytest.raises(ValidationError) as info:
                call()
            assert info.value.code == "version_immutable"

    def test_publishing_a_newer_version_supersedes_the_previous_one(self, published):
        author = published["author"]
        v1 = published["v1"]
        v2 = lifecycle.create_draft(published["template"], actor=author, change_summary="Tighten range")
        assert (v2.version_number, v2.status, v2.based_on_id) == (2, "draft", v1.pk)
        services.update_item(item_of(v2, "FRIDGE-TEMP"), actor=author, config={"min": 0, "max": 8, "decimals": 1, "unit": "°C"})
        lifecycle.publish_version(v2, actor=author)
        v1.refresh_from_db(), v2.refresh_from_db()
        assert (v1.status, v2.status) == ("superseded", "published") and v1.superseded_at and v2.superseded_at is None
        assert ChecklistVersion.objects.filter(template=published["template"], status="published").count() == 1
        assert selectors.current_published_version(published["template"]).pk == v2.pk
        assert verbs().count("checklist_version.superseded") == 1

    def test_the_database_allows_only_one_published_version_per_template(self, published):
        v1 = published["v1"]
        v2 = lifecycle.create_draft(published["template"], actor=published["author"])
        with pytest.raises(IntegrityError), transaction.atomic():
            ChecklistVersion.objects.filter(pk=v2.pk).update(status="published", published_at=v1.published_at)

    def test_publishing_twice_is_a_conflict_and_records_nothing_more(self, published):
        v1 = published["v1"]
        before = verbs().count("checklist_version.published")
        with pytest.raises(lifecycle.ConflictError) as info:
            lifecycle.publish_version(v1, actor=published["author"])
        assert info.value.lifecycle_code == "already_published"
        assert verbs().count("checklist_version.published") == before

    def test_a_stale_draft_hash_is_refused(self, world):
        draft, author = world["draft"], world["author"]
        token = lifecycle.run_validation(draft, actor=author).draft_hash
        services.update_item(item_of(draft, "ISSUE"), actor=author, question="Describe the issue in detail.")
        with pytest.raises(lifecycle.ConflictError) as info:
            lifecycle.publish_version(draft, actor=author, expected_hash=token)
        assert info.value.lifecycle_code == "stale_draft"
        draft.refresh_from_db()
        assert draft.status == "draft"
        lifecycle.publish_version(draft, actor=author, expected_hash=lifecycle.run_validation(draft).draft_hash)  # re-validated: fine

    def test_transition_table(self):
        assert lifecycle.ALLOWED_TRANSITIONS["draft"] == {"published"} and lifecycle.ALLOWED_TRANSITIONS["published"] == {"superseded"}
        assert lifecycle.ALLOWED_TRANSITIONS["superseded"] == frozenset()
        for old, new in (("published", "draft"), ("superseded", "published"), ("superseded", "draft"), ("draft", "superseded")):
            with pytest.raises(lifecycle.LifecycleError) as info:
                lifecycle.assert_transition(old, new)
            assert info.value.lifecycle_code == "invalid_transition"

    def test_a_version_of_another_template_is_refused(self, published, org_a):
        other_template = services.create_checklist_template(organization=org_a, name="Other", code="OTH-1")
        with pytest.raises(ValidationError):
            lifecycle.create_draft(other_template, source=published["v1"])
        with pytest.raises(lifecycle.LifecycleError):
            lifecycle.compare_versions(other_template, published["v1"], published["v1"])


@pytest.mark.django_db
class TestDraftsAndRollback:
    def test_one_draft_per_template(self, published):
        lifecycle.create_draft(published["template"], actor=published["author"])
        with pytest.raises(lifecycle.LifecycleError) as info:
            lifecycle.create_draft(published["template"], actor=published["author"])
        assert info.value.lifecycle_code == "draft_exists"

    def test_discarding_a_draft_deletes_it_and_leaves_published_versions_alone(self, published):
        template, author = published["template"], published["author"]
        v2 = lifecycle.create_draft(template, actor=author)
        children = published["v1"].sections.count()
        lifecycle.discard_draft(v2, actor=author)
        assert not ChecklistVersion.objects.filter(pk=v2.pk).exists()
        assert published["v1"].sections.count() == children and published["v1"].status == "published"
        assert "checklist_version.discarded" in verbs()
        assert lifecycle.create_draft(template, actor=author).version_number == 2  # a discarded draft's number is reused

    def test_a_published_version_cannot_be_discarded(self, published):
        with pytest.raises(lifecycle.LifecycleError) as info:
            lifecycle.discard_draft(published["v1"], actor=published["author"])
        assert info.value.lifecycle_code == "not_draft" and published["v1"].sections.count() == 3

    def test_the_only_version_cannot_be_discarded(self, world):
        with pytest.raises(lifecycle.LifecycleError) as info:
            lifecycle.discard_draft(world["draft"], actor=world["author"])
        assert info.value.lifecycle_code == "only_version"

    def test_rollback_is_a_new_draft_from_a_historical_version(self, published):
        template, author = published["template"], published["author"]
        v1 = published["v1"]
        v2 = lifecycle.create_draft(template, actor=author)
        services.update_item(item_of(v2, "FRIDGE-TEMP"), actor=author, config={"min": 0, "max": 8, "decimals": 1, "unit": "°C"})
        lifecycle.publish_version(v2, actor=author)
        v3 = lifecycle.create_draft(template, actor=author, source=v1, change_summary="Roll back to v1")  # v1 is superseded
        assert v3.based_on_id == v1.pk and item_of(v3, "FRIDGE-TEMP").config["max"] == 10
        lifecycle.publish_version(v3, actor=author)
        statuses = {v.version_number: v.status for v in template.versions.all()}
        assert statuses == {1: "superseded", 2: "superseded", 3: "published"}  # history is not rewritten: v1 is not republished
        v1.refresh_from_db()
        assert item_of(v1, "FRIDGE-TEMP").config["max"] == 10 and v1.status == "superseded"

    def test_a_draft_cannot_be_the_base_of_another(self, published):
        draft = lifecycle.create_draft(published["template"], actor=published["author"])
        with pytest.raises(lifecycle.LifecycleError):
            lifecycle.create_draft(published["template"], actor=published["author"], source=draft)


@pytest.mark.django_db
class TestArchivalAndAssignability:
    def test_an_archived_template_refuses_new_drafts_publication_and_edits(self, published):
        template, author = published["template"], published["author"]
        draft = lifecycle.create_draft(template, actor=author)
        services.archive_checklist_template(template, actor=author)
        with pytest.raises(lifecycle.LifecycleError) as info:
            lifecycle.create_draft(template, actor=author)
        assert info.value.lifecycle_code in ("template_archived", "draft_exists")
        with pytest.raises(ValidationError):
            lifecycle.publish_version(draft, actor=author)
        with pytest.raises(ValidationError):
            services.create_section(draft, name="X", actor=author)
        with pytest.raises(ValidationError):
            lifecycle.discard_draft(draft, actor=author)

    def test_archiving_deletes_nothing_and_restore_is_an_explicit_transition(self, published):
        template, author = published["template"], published["author"]
        services.archive_checklist_template(template, actor=author)
        assert template.versions.count() == 1 and published["v1"].sections.count() == 3
        with pytest.raises(lifecycle.LifecycleError) as info:
            lifecycle.create_draft(template, actor=author)
        assert info.value.lifecycle_code == "template_archived" and "archived" in str(info.value)
        services.restore_checklist_template(template, actor=author)
        template.refresh_from_db()
        assert template.status == "active" and lifecycle.create_draft(template, actor=author).version_number == 2
        assert {"checklist.archived", "checklist.restored"} <= set(verbs())

    def test_only_the_current_version_of_an_active_template_is_assignable(self, published):
        template, author, v1 = published["template"], published["author"], published["v1"]
        assert lifecycle.is_assignable(v1)
        v2 = lifecycle.create_draft(template, actor=author)
        assert not lifecycle.is_assignable(v2)  # a draft
        lifecycle.publish_version(v2, actor=author)
        v1.refresh_from_db()
        with pytest.raises(lifecycle.LifecycleError) as info:
            lifecycle.assert_assignable(v1)
        assert info.value.lifecycle_code == "superseded" and lifecycle.is_assignable(v2)
        services.archive_checklist_template(template, actor=author)
        v2.refresh_from_db()
        with pytest.raises(lifecycle.LifecycleError) as info:
            lifecycle.assert_assignable(v2)
        assert info.value.lifecycle_code == "template_archived"

    def test_a_template_that_was_never_activated_is_not_assignable(self, world):
        published_version = services.publish_checklist_version(world["draft"], actor=world["author"])
        with pytest.raises(lifecycle.LifecycleError) as info:
            lifecycle.assert_assignable(published_version)
        assert info.value.lifecycle_code == "template_not_active"


@pytest.mark.django_db
class TestAuditTrail:
    def test_the_events_of_a_release_cycle(self, published):
        template, author = published["template"], published["author"]
        v2 = lifecycle.create_draft(template, actor=author)
        lifecycle.run_validation(v2, actor=author)
        lifecycle.publish_version(v2, actor=author)
        lifecycle.compare_versions(template, published["v1"], v2, actor=author)
        got = set(verbs())
        assert {"checklist_version.created", "checklist_version.cloned", "checklist_version.validated", "checklist_version.published",
                "checklist_version.superseded", "checklist_version.compared"} <= got
        published_event = ActivityLog.objects.filter(verb="checklist_version.published").order_by("-created_at").first()
        assert published_event.actor_id == author.pk and published_event.metadata["version"] == 2 and published_event.metadata["template_id"] == str(template.pk)
        cloned = ActivityLog.objects.get(verb="checklist_version.cloned")
        assert cloned.metadata["source_version"] == 1 and cloned.metadata["items"] == 5

    def test_history_rows_record_the_publication(self, published):
        v1 = published["v1"]
        statuses = list(v1.history.order_by("history_date").values_list("status", flat=True))
        assert statuses[0] == "draft" and statuses[-1] == "published"
        assert v1.history.first().history_user_id == published["author"].pk
