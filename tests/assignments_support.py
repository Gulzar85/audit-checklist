"""Builders for the Phase 8 tests: an organization with a location tree, a published checklist and users with scoped roles."""

from __future__ import annotations

from datetime import timedelta

from django.utils import timezone

from apps.checklists import lifecycle, services as checklist_services
from apps.locations import services as location_services
from apps.permissions import services as rbac
from apps.permissions.models import MembershipRole
from apps.permissions.registry import P

ASSIGN_ALL = [P.ASSIGNMENT_VIEW, P.ASSIGNMENT_CREATE, P.ASSIGNMENT_ISSUE, P.ASSIGNMENT_UPDATE, P.ASSIGNMENT_CANCEL, P.ASSIGNMENT_REOPEN,
              P.ASSIGNMENT_BULK_CREATE, P.LOCATION_VIEW, P.EXECUTION_CREATE, P.EXECUTION_VIEW]


def future(hours=24):
    return timezone.now() + timedelta(hours=hours)


def publish_checklist(organization, actor, code="RSC", name="Restaurant Daily Safety Checklist"):
    template = checklist_services.create_checklist_template(organization=organization, name=name, code=code, actor=actor)
    draft = template.versions.get()
    section = checklist_services.create_section(draft, name="Safety", actor=actor)
    checklist_services.create_item(section, question="Is the fire exit accessible?", code="FIRE", answer_type="yes_no", actor=actor)
    version = checklist_services.publish_checklist_version(draft, actor=actor)
    checklist_services.activate_checklist_template(template, actor=actor)
    version.refresh_from_db()
    return template, version


def build_world(org_a, org_b, make_user, assign, make_type):
    """Region > Area 1 > (Restaurant 1, Restaurant 2); Area 2 > Restaurant 3; a Warehouse; roles scoped to Area 1."""
    admin = make_user(email="as-admin@example.com", organization=org_a, role=None)
    assign(admin, org_a, "administrator")
    other = make_user(email="as-other@example.com", organization=org_b, role=None)
    assign(other, org_b, "administrator")

    region_t, area_t, rest_t, wh_t = (make_type(org_a, n, n.upper()) for n in ("Region", "Area", "Restaurant", "Warehouse"))
    lc = location_services.create_location
    region = lc(organization=org_a, name="Region North", code="REG-N", location_type=region_t)
    area1 = lc(organization=org_a, name="Area 1", code="AREA-1", location_type=area_t, parent=region)
    area2 = lc(organization=org_a, name="Area 2", code="AREA-2", location_type=area_t, parent=region)
    r1 = lc(organization=org_a, name="Restaurant 1", code="R-1", location_type=rest_t, parent=area1)
    r2 = lc(organization=org_a, name="Restaurant 2", code="R-2", location_type=rest_t, parent=area1)
    r3 = lc(organization=org_a, name="Restaurant 3", code="R-3", location_type=rest_t, parent=area2)
    wh = lc(organization=org_a, name="Warehouse 1", code="WH-1", location_type=wh_t)
    b_root_t = make_type(org_b, "Site", "SITE")
    b_loc = lc(organization=org_b, name="B Site", code="B-1", location_type=b_root_t)

    checklist_viewer = rbac.create_role(organization=org_a, name="Checklist viewer", permissions=[P.CHECKLIST_VIEW])
    assigner = rbac.create_role(organization=org_a, name="Assigner", permissions=ASSIGN_ALL)
    area_manager = make_user(email="as-manager@example.com", organization=org_a, role=None)  # may assign inside Area 1 only
    assign(area_manager, org_a, checklist_viewer)
    assign(area_manager, org_a, assigner, scope=MembershipRole.ScopeType.LOCATION_DESCENDANTS, location=area1)
    worker = make_user(email="as-worker@example.com", organization=org_a, role=None)  # may run checklists at Restaurant 1
    assign(worker, org_a, rbac.create_role(organization=org_a, name="Worker", permissions=[P.EXECUTION_CREATE, P.EXECUTION_VIEW, P.LOCATION_VIEW]),
           scope=MembershipRole.ScopeType.LOCATION, location=r1)
    viewer = make_user(email="as-viewer@example.com", organization=org_a, role=None)
    assign(viewer, org_a, rbac.create_role(organization=org_a, name="Assignment viewer", permissions=[P.ASSIGNMENT_VIEW, P.LOCATION_VIEW]))
    plain = make_user(email="as-plain@example.com", organization=org_a, role=None)

    template, version = publish_checklist(org_a, admin)
    b_template, b_version = publish_checklist(org_b, other, code="BSC", name="B checklist")
    return {
        "admin": admin, "other": other, "manager": area_manager, "worker": worker, "viewer": viewer, "plain": plain,
        "region": region, "area1": area1, "area2": area2, "r1": r1, "r2": r2, "r3": r3, "wh": wh, "b_loc": b_loc,
        "template": template, "version": version, "b_template": b_template, "b_version": b_version, "org": org_a,
    }
