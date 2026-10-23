"""Phase 12 security: organization isolation, location scope, visibility, database guards and constraints, admin."""

from __future__ import annotations

import pytest
from django.db import IntegrityError, transaction
from django.urls import reverse
from django.utils import timezone

from apps.findings import policies, selectors, services
from apps.findings.models import Finding, FindingCategory, FindingSeverity

from findings_support import build_findings_world, submit
from test_findings_lifecycle import cat, manual, sev


@pytest.fixture
def w(org_a, org_b, make_user, assign, make_type):
    world = build_findings_world(org_a, org_b, make_user, assign, make_type)
    world["b_admin"] = make_user(email="f-b-admin@example.com", organization=org_b, role="administrator")
    services.ensure_defaults(org_b)
    return world


def visible(user):
    return set(selectors.findings_visible_to(user).values_list("pk", flat=True))


class TestVisibility:
    def test_scope_and_organization_isolation(self, w):
        f1, f2 = manual(w, who="admin"), manual(w, who="admin", location=w["r2"])
        assert visible(w["viewer"]) == {f1.pk}          # scoped to Restaurant 1
        assert visible(w["r2_viewer"]) == {f2.pk}       # scoped to Restaurant 2
        assert visible(w["admin"]) == {f1.pk, f2.pk}
        assert visible(w["b_admin"]) == set()           # another organization sees nothing
        assert selectors.get_finding(w["b_admin"], f1.pk) is None and selectors.get_finding(w["viewer"], f2.pk) is None
        assert selectors.get_finding(w["viewer"], "not-a-uuid") is None

    def test_a_user_without_the_permission_sees_nothing_but_their_own_assignments(self, w, make_user):
        nobody = make_user(email="f-nobody@example.com", organization=w["org_a"], role=None)
        f = manual(w, who="admin", assigned_to=w["triager"])
        assert visible(nobody) == set()
        assert visible(w["triager"]) == {f.pk}
        assert policies.can_view(w["triager"], f)

    def test_inactive_and_anonymous_users_see_nothing(self, w):
        manual(w, who="admin")
        from apps.accounts.constants import UserStatus

        w["viewer"].status = UserStatus.SUSPENDED
        w["viewer"].save()
        assert visible(w["viewer"]) == set()
        from django.contrib.auth.models import AnonymousUser

        assert not selectors.findings_visible_to(AnonymousUser()).exists()

    def test_the_summary_is_scoped(self, w):
        manual(w, who="admin")
        manual(w, who="admin", location=w["r2"])
        assert selectors.summary(selectors.findings_visible_to(w["viewer"]))["open"] == 1
        assert selectors.summary(selectors.findings_visible_to(w["admin"]))["open"] == 2
        assert selectors.summary(selectors.findings_visible_to(w["b_admin"]))["total"] == 0

    def test_category_and_severity_choices_never_cross_organizations(self, w, org_a):
        assert set(selectors.visible_categories(w["b_admin"]).values_list("organization_id", flat=True)) == {w["org_b"].pk}
        assert set(selectors.visible_severities(w["viewer"]).values_list("organization_id", flat=True)) == {org_a.pk}


class TestEligibleAssignees:
    def test_only_active_members_who_can_work_findings_at_the_location(self, w):
        users = set(selectors.eligible_assignees(w["org_a"], w["r1"]))
        assert w["triager"] in users and w["editor"] in users and w["admin"] in users
        assert w["viewer"] not in users and w["r2_viewer"] not in users and w["b_admin"] not in users


@pytest.mark.django_db
class TestDatabaseGuards:
    def test_a_finding_can_never_be_deleted(self, w):
        f = manual(w)
        with pytest.raises(IntegrityError), transaction.atomic():
            Finding.objects.filter(pk=f.pk).delete()
        with pytest.raises(IntegrityError), transaction.atomic():
            Finding.objects.filter(pk=f.pk)._raw_delete(Finding.objects.db)
        assert Finding.objects.filter(pk=f.pk).exists()

    def test_the_source_traceability_never_changes(self, w):
        ex = submit(w, {"EXIT": "no"})
        f = Finding.objects.get(execution=ex)
        other = submit(w, {"EXIT": "yes"}, location=w["r2"], who="area_runner")
        for change in ({"execution": other}, {"number": 99}, {"location": w["r2"]}, {"detection_key": "x"}, {"rule_code": "x"}, {"item": None},
                       {"detected_at": timezone.now()}, {"source_type": "manual"}):
            with pytest.raises(IntegrityError), transaction.atomic():
                Finding.objects.filter(pk=f.pk).update(**change)

    def test_relationships_must_agree(self, w, org_b):
        f = manual(w)
        foreign_cat = FindingCategory.objects.get(organization=org_b, code="safety")
        foreign_sev = FindingSeverity.objects.get(organization=org_b, code="low")
        for change in ({"category": foreign_cat}, {"severity": foreign_sev}):
            with pytest.raises(IntegrityError), transaction.atomic():
                Finding.objects.filter(pk=f.pk).update(**change)
        ex = submit(w, {"EXIT": "yes"})
        with pytest.raises(IntegrityError), transaction.atomic():  # an execution of Restaurant 1 cannot belong to a finding at Restaurant 2
            Finding.objects.create(organization=w["org_a"], number=50, title="x", category=cat(w), severity=sev(w), source_type="manual", location=w["r2"],
                                   execution=ex, assignment=ex.assignment, version=ex.version)

    def test_constraints_protect_the_invariants(self, w):
        ex = submit(w, {"EXIT": "no"})
        f = Finding.objects.get(execution=ex)
        with pytest.raises(IntegrityError), transaction.atomic():  # one finding per trigger
            Finding.objects.create(organization=f.organization, number=77, title="dup", category=f.category, severity=f.severity, source_type=f.source_type,
                                   location=f.location, assignment=f.assignment, execution=f.execution, version=f.version, item=f.item, rule_code=f.rule_code,
                                   detection_key=f.detection_key)
        with pytest.raises(IntegrityError), transaction.atomic():  # a rule finding needs its whole source
            Finding.objects.create(organization=f.organization, number=78, title="x", category=f.category, severity=f.severity, source_type="checklist_rule",
                                   location=f.location)
        with pytest.raises(IntegrityError), transaction.atomic():  # cancelled needs a reason
            Finding.objects.filter(pk=f.pk).update(status="cancelled", cancelled_at=timezone.now(), cancel_reason="")
        with pytest.raises(IntegrityError), transaction.atomic():  # closed needs its timestamp
            Finding.objects.filter(pk=f.pk).update(status="closed", resolved_at=timezone.now())
        with pytest.raises(IntegrityError), transaction.atomic():
            Finding.objects.filter(pk=f.pk).update(status="bogus")
        with pytest.raises(IntegrityError), transaction.atomic():  # numbers are unique per organization
            Finding.objects.filter(pk=manual(w).pk).update(number=f.number)

    def test_severity_and_category_configuration_constraints(self, w):
        with pytest.raises(IntegrityError), transaction.atomic():
            FindingSeverity.objects.filter(organization=w["org_a"], code="low").update(rank=20)  # rank is unique per organization
        with pytest.raises(IntegrityError), transaction.atomic():
            FindingSeverity.objects.filter(organization=w["org_a"], code="low").update(is_default=True)  # one default
        with pytest.raises(IntegrityError), transaction.atomic():
            FindingCategory.objects.filter(organization=w["org_a"], code="other").update(code="Bad Code")
        with pytest.raises(IntegrityError), transaction.atomic():
            FindingSeverity.objects.filter(organization=w["org_a"], code="low").update(rank=500)

    def test_a_used_category_or_severity_cannot_be_deleted(self, w):
        from django.db.models import ProtectedError

        f = manual(w)
        with pytest.raises(ProtectedError):
            f.category.delete()
        with pytest.raises(ProtectedError):
            f.severity.delete()


@pytest.mark.django_db
class TestAdmin:
    def test_the_admin_is_read_only(self, client, w, django_user_model):
        f = manual(w)
        su = django_user_model.objects.create_superuser(email="su-find@example.com", password="x-Strong-pass-123")
        client.force_login(su)
        for model in ("finding", "findingcategory", "findingseverity"):
            assert client.get(reverse(f"admin:findings_{model}_changelist")).status_code == 200
            assert client.get(reverse(f"admin:findings_{model}_add")).status_code == 403
        page = client.get(reverse("admin:findings_finding_change", args=[f.pk])).content.decode()
        assert 'name="_save"' not in page and f.title in page
        assert client.post(reverse("admin:findings_finding_delete", args=[f.pk]), {"post": "yes"}).status_code == 403
        assert client.post(reverse("admin:findings_finding_change", args=[f.pk]), {"status": "closed", "title": "hacked"}).status_code in (302, 403)
        f.refresh_from_db()
        assert f.status == "open" and f.title != "hacked"

    def test_a_scoped_staff_member_only_sees_their_scope(self, client, w):
        f1, f2 = manual(w, who="admin"), manual(w, who="admin", location=w["r2"])
        w["viewer"].is_staff = True
        w["viewer"].save()
        from apps.findings.admin import FindingAdmin
        from django.contrib import admin
        from django.test import RequestFactory

        request = RequestFactory().get("/")
        request.user = w["viewer"]
        assert set(FindingAdmin(Finding, admin.site).get_queryset(request).values_list("pk", flat=True)) == {f1.pk}
        _ = f2
