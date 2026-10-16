"""Concurrency of the version lifecycle: real threads against PostgreSQL (row locks, partial unique indexes)."""

from __future__ import annotations

import threading

import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError, connection

from apps.checklists import lifecycle, services
from apps.checklists.manifest import manifest_hash, verify_version_integrity
from apps.checklists.models import ChecklistVersion
from apps.core.models import ActivityLog

from versioning_support import build_world, item_of


def run_together(*jobs):
    """Run each callable in its own thread, released at the same instant; return their results (or the exception raised)."""
    barrier = threading.Barrier(len(jobs))
    results: list = [None] * len(jobs)

    def runner(index, job):
        try:
            barrier.wait(timeout=15)
            results[index] = job()
        except Exception as exc:  # noqa: BLE001 - the test inspects what each racer saw
            results[index] = exc
        finally:
            connection.close()

    threads = [threading.Thread(target=runner, args=(i, job)) for i, job in enumerate(jobs)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    return results


@pytest.fixture
def world(org_a, org_b, make_user, assign):
    return build_world(org_a, org_b, make_user, assign)


@pytest.mark.django_db(transaction=True)
class TestConcurrentPublication:
    def test_two_simultaneous_publications_of_one_draft(self, world):
        draft_id, author_id = world["draft"].pk, world["author"].pk
        from django.contrib.auth import get_user_model

        def publish():
            return lifecycle.publish_version(ChecklistVersion.objects.get(pk=draft_id), actor=get_user_model().objects.get(pk=author_id))

        results = run_together(publish, publish)
        winners = [r for r in results if isinstance(r, ChecklistVersion)]
        losers = [r for r in results if isinstance(r, lifecycle.ConflictError)]
        assert len(winners) == 1 and len(losers) == 1 and losers[0].lifecycle_code == "already_published", results
        assert ActivityLog.objects.filter(verb="checklist_version.published", metadata__template_id=str(world["template"].pk)).count() == 1
        version = ChecklistVersion.objects.get(pk=draft_id)
        assert version.status == "published" and verify_version_integrity(version).ok

    def test_repeated_publish_requests_are_idempotent(self, world):
        version = lifecycle.publish_version(world["draft"], actor=world["author"])
        for _ in range(3):
            with pytest.raises(lifecycle.ConflictError):
                lifecycle.publish_version(version, actor=world["author"])
        assert ActivityLog.objects.filter(verb="checklist_version.published", metadata__template_id=str(world["template"].pk)).count() == 1
        assert ChecklistVersion.objects.filter(template=world["template"], status="published").count() == 1

    def test_publish_racing_an_edit_never_publishes_an_unreviewed_change(self, world):
        draft_id, item_id, author_id = world["draft"].pk, item_of(world["draft"], "FIRE-EXIT").pk, world["author"].pk
        from django.contrib.auth import get_user_model

        def publish():
            return lifecycle.publish_version(ChecklistVersion.objects.get(pk=draft_id), actor=get_user_model().objects.get(pk=author_id))

        def edit():
            from apps.checklists.models import ChecklistItem

            return services.update_item(ChecklistItem.objects.get(pk=item_id), actor=get_user_model().objects.get(pk=author_id), question="Edited while publishing")

        results = run_together(publish, edit)
        version = ChecklistVersion.objects.get(pk=draft_id)
        assert version.status == "published"  # the publication always wins or is first; the edit is either included or refused
        edit_result = results[1]
        assert isinstance(edit_result, (ValidationError, type(item_of(version, "FIRE-EXIT")))), results
        # Whatever happened, the sealed hash describes exactly the rows that exist: nothing slipped in after publication.
        assert verify_version_integrity(version).ok
        if isinstance(edit_result, ValidationError):
            assert edit_result.code == "version_immutable" and item_of(version, "FIRE-EXIT").question == "Is the fire exit accessible?"
        else:
            assert item_of(version, "FIRE-EXIT").question == "Edited while publishing"

    def test_a_draft_that_changed_since_it_was_validated_is_a_conflict(self, world):
        token = lifecycle.run_validation(world["draft"]).draft_hash
        services.update_item(item_of(world["draft"], "FIRE-EXIT"), actor=world["author"], question="Changed after validation")
        with pytest.raises(lifecycle.ConflictError) as info:
            lifecycle.publish_version(world["draft"], actor=world["author"], expected_hash=token)
        assert info.value.lifecycle_code == "stale_draft" and ChecklistVersion.objects.get(pk=world["draft"].pk).status == "draft"


@pytest.mark.django_db(transaction=True)
class TestConcurrentDrafts:
    def test_two_simultaneous_draft_creations_yield_one_draft(self, org_a, org_b, make_user, assign):
        world = build_world(org_a, org_b, make_user, assign, publish=True)
        template_id, author_id = world["template"].pk, world["author"].pk
        from django.contrib.auth import get_user_model
        from apps.checklists.models import ChecklistTemplate

        def create():
            return lifecycle.create_draft(ChecklistTemplate.objects.get(pk=template_id), actor=get_user_model().objects.get(pk=author_id))

        results = run_together(create, create)
        created = [r for r in results if isinstance(r, ChecklistVersion)]
        refused = [r for r in results if isinstance(r, lifecycle.LifecycleError)]
        assert len(created) == 1 and len(refused) == 1, results
        assert refused[0].lifecycle_code in ("draft_exists",)
        numbers = list(ChecklistVersion.objects.filter(template_id=template_id).order_by("version_number").values_list("version_number", flat=True))
        assert numbers == [1, 2] and ChecklistVersion.objects.filter(template_id=template_id, status="draft").count() == 1

    def test_version_numbers_are_unique_and_sequential(self, org_a, org_b, make_user, assign):
        world = build_world(org_a, org_b, make_user, assign, publish=True)
        template, author = world["template"], world["author"]
        for expected in (2, 3, 4):
            draft = lifecycle.create_draft(template, actor=author)
            assert draft.version_number == expected
            lifecycle.publish_version(draft, actor=author)
        numbers = list(template.versions.order_by("version_number").values_list("version_number", flat=True))
        assert numbers == [1, 2, 3, 4] and template.versions.filter(status="published").count() == 1
        with pytest.raises(IntegrityError):
            ChecklistVersion.objects.create(template=template, version_number=3)  # a duplicate number is impossible

    def test_publication_serialises_with_draft_creation(self, org_a, org_b, make_user, assign):
        """Publishing v2 while another request tries to start v3: v3 can only be created once v2 is no longer a draft."""
        world = build_world(org_a, org_b, make_user, assign, publish=True)
        template_id, author_id = world["template"].pk, world["author"].pk
        from django.contrib.auth import get_user_model
        from apps.checklists.models import ChecklistTemplate

        v2 = lifecycle.create_draft(world["template"], actor=world["author"])

        def publish():
            return lifecycle.publish_version(ChecklistVersion.objects.get(pk=v2.pk), actor=get_user_model().objects.get(pk=author_id))

        def create():
            return lifecycle.create_draft(ChecklistTemplate.objects.get(pk=template_id), actor=get_user_model().objects.get(pk=author_id))

        results = run_together(publish, create)
        statuses = {v.version_number: v.status for v in ChecklistVersion.objects.filter(template_id=template_id)}
        assert statuses[2] == "published" and statuses[1] == "superseded"
        assert sum(1 for s in statuses.values() if s == "draft") <= 1 and sum(1 for s in statuses.values() if s == "published") == 1, (results, statuses)
