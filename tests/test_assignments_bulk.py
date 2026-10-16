"""Bulk assignment: all-or-nothing, per-location results, scope, duplicates, repeat submissions and races."""

from __future__ import annotations

import threading

import pytest
from django.db import connection

from apps.assignments import selectors, services
from apps.assignments.models import ChecklistAssignment
from apps.assignments.services import AssignmentError
from apps.core.models import ActivityLog

from assignments_support import build_world, future


@pytest.fixture
def w(org_a, org_b, make_user, assign, make_type):
    return build_world(org_a, org_b, make_user, assign, make_type)


def bulk(w, locations, **kw):
    args = {"actor": w["admin"], "version": w["version"], "location_ids": [str(x.pk) for x in locations]}
    args.update(kw)
    return services.bulk_create_assignments(**args)


@pytest.mark.django_db
class TestHappyPath:
    def test_every_eligible_location_receives_an_assignment(self, w):
        result = bulk(w, [w["r1"], w["r2"], w["wh"]], issue=True, due_at=future(48), priority="high", instructions="Quarterly push")
        assert result.created and result.count == 3 and all(r.ok for r in result.results)
        rows = ChecklistAssignment.objects.order_by("number")
        assert [a.status for a in rows] == ["assigned"] * 3 and {a.priority for a in rows} == {"high"} and {a.version_id for a in rows} == {w["version"].pk}
        assert {a.location_id for a in rows} == {w["r1"].pk, w["r2"].pk, w["wh"].pk}  # a warehouse works like a restaurant
        assert ActivityLog.objects.filter(verb="assignment.bulk_created").count() == 1

    def test_drafts_can_be_created_in_bulk(self, w):
        result = bulk(w, [w["r1"], w["r2"]], issue=False)
        assert result.created and {a.status for a in ChecklistAssignment.objects.all()} == {"draft"}

    def test_dry_run_changes_nothing_but_reports(self, w):
        result = bulk(w, [w["r1"], w["r2"]], dry_run=True)
        assert not result.created and all(r.ok for r in result.results) and not ChecklistAssignment.objects.exists()

    def test_duplicate_ids_in_the_request_are_collapsed(self, w):
        assert bulk(w, [w["r1"], w["r1"]]).count == 1


@pytest.mark.django_db
class TestAllOrNothing:
    def test_one_invalid_target_creates_nothing_and_says_which(self, w):
        services.create_assignment(actor=w["admin"], version=w["version"], location=w["r2"], issue=True)
        result = bulk(w, [w["r1"], w["r2"], w["wh"]], issue=True)
        assert not result.created and result.count == 0
        by_name = {r.name: r for r in result.results}
        assert by_name["Region North / Area 1 / Restaurant 1"].ok and by_name["Warehouse 1"].ok
        failed = by_name["Region North / Area 1 / Restaurant 2"]
        assert not failed.ok and "ASG-00001" in failed.errors[0]
        assert ChecklistAssignment.objects.count() == 1  # only the pre-existing one

    def test_a_repeated_submission_creates_no_duplicates(self, w):
        assert bulk(w, [w["r1"], w["r2"]], issue=True).created
        again = bulk(w, [w["r1"], w["r2"]], issue=True)
        assert not again.created and all(not r.ok for r in again.results)
        assert ChecklistAssignment.objects.count() == 2

    def test_repeated_draft_submissions_are_caught_too(self, w):
        assert bulk(w, [w["r1"]]).created
        assert not bulk(w, [w["r1"]]).created and ChecklistAssignment.objects.count() == 1

    def test_validation_applies_to_every_target(self, w):
        assert not bulk(w, [w["r1"]], due_at=future(-1)).created
        assert not bulk(w, [w["r1"]], priority="nope").created
        result = bulk(w, [w["r1"], w["r2"]], assigned_to=w["worker"])  # the worker may run checklists at Restaurant 1 only
        assert not result.created and [r.ok for r in result.results] == [True, False]

    def test_limits_and_empty_requests(self, w):
        with pytest.raises(AssignmentError):
            bulk(w, [])
        with pytest.raises(AssignmentError):
            services.bulk_create_assignments(actor=w["admin"], version=w["version"], location_ids=[str(i) for i in range(500)])

    def test_an_unpublished_or_superseded_version_fails_every_target(self, w):
        from apps.checklists import lifecycle

        v2 = lifecycle.create_draft(w["template"], actor=w["admin"])
        lifecycle.publish_version(v2, actor=w["admin"])
        result = bulk(w, [w["r1"], w["r2"]])  # the original version is superseded now
        assert not result.created and all("superseded" in " ".join(r.errors) for r in result.results)


@pytest.mark.django_db
class TestScopeAndDisclosure:
    def test_out_of_scope_foreign_and_unknown_targets_are_generic(self, w):
        result = services.bulk_create_assignments(
            actor=w["manager"], version=w["version"],
            location_ids=[str(w["r1"].pk), str(w["r3"].pk), str(w["b_loc"].pk), "00000000-0000-0000-0000-000000000000", "not-a-uuid"])
        assert not result.created and ChecklistAssignment.objects.count() == 0
        entries = result.results
        assert entries[0].ok
        generic = {tuple(r.errors) for r in entries[1:]}
        assert len(generic) == 1 and "not available" in next(iter(generic))[0]  # identical for out-of-scope, other org, unknown and malformed
        assert all(r.name == "(not available)" for r in entries[1:])

    def test_a_scoped_manager_can_bulk_assign_inside_their_scope(self, w):
        assert services.bulk_create_assignments(actor=w["manager"], version=w["version"], location_ids=[str(w["r1"].pk), str(w["r2"].pk)]).created

    def test_a_user_without_the_permission_gets_only_refusals(self, w):
        result = services.bulk_create_assignments(actor=w["viewer"], version=w["version"], location_ids=[str(w["r1"].pk)])
        assert not result.created and not ChecklistAssignment.objects.exists()

    def test_the_other_organization_cannot_use_our_version(self, w):
        result = services.bulk_create_assignments(actor=w["other"], version=w["version"], location_ids=[str(w["b_loc"].pk)])
        assert not result.created and "does not belong" in " ".join(result.results[0].errors)

    def test_expanding_descendants_never_includes_inaccessible_locations(self, w):
        locations, skipped = selectors.expand_targets(w["manager"], [w["region"]], include_descendants=True)
        names = {loc.name for loc in locations}
        assert names == {"Area 1", "Restaurant 1", "Restaurant 2"}  # only what the manager may reach; the region itself is not theirs
        assert skipped == 2  # Area 2 and Restaurant 3 were left out - and counted, never silently selected
        locations, skipped = selectors.expand_targets(w["manager"], [w["area1"]], include_descendants=True)
        assert {loc.name for loc in locations} == {"Area 1", "Restaurant 1", "Restaurant 2"} and skipped == 0
        locations, skipped = selectors.expand_targets(w["admin"], [w["region"]], include_descendants=True)
        assert {loc.name for loc in locations} >= {"Region North", "Area 2", "Restaurant 3"}
        _all, skipped = selectors.expand_targets(w["manager"], [w["area1"]], include_descendants=False)
        assert skipped == 0


@pytest.mark.django_db(transaction=True)
class TestConcurrency:
    def test_two_simultaneous_bulk_requests_yield_one_set(self, org_a, org_b, make_user, assign, make_type):
        w = build_world(org_a, org_b, make_user, assign, make_type)
        admin_id, version_id = w["admin"].pk, w["version"].pk
        ids = [str(w["r1"].pk), str(w["r2"].pk)]
        from django.contrib.auth import get_user_model
        from apps.checklists.models import ChecklistVersion

        barrier = threading.Barrier(2)
        out = []

        def run():
            try:
                barrier.wait(timeout=15)
                out.append(services.bulk_create_assignments(
                    actor=get_user_model().objects.get(pk=admin_id), version=ChecklistVersion.objects.get(pk=version_id), location_ids=ids, issue=True))
            except Exception as exc:  # noqa: BLE001
                out.append(exc)
            finally:
                connection.close()

        threads = [threading.Thread(target=run) for _ in range(2)]
        [t.start() for t in threads]
        [t.join(timeout=60) for t in threads]
        assert ChecklistAssignment.objects.filter(status="assigned").count() == 2, out
        assert sorted(isinstance(r, services.BulkResult) and r.created for r in out) == [False, True]
