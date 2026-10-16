"""Shared builders for the Phase 7 (versioning) tests: one realistic checklist and the users around it."""

from __future__ import annotations

from apps.checklists import services
from apps.permissions import services as rbac
from apps.permissions.registry import P


def act(kind, target=None, target_type="item", **params):
    out = {"type": kind, **params}
    if target:
        out["target"] = {"type": target_type, "code": target}
    return out


def build_world(org_a, org_b, make_user, assign, *, publish=False):
    """Users: ``author`` (administrator), ``editor`` (view+create+update), ``publisher`` (view+publish), ``reader`` (view),
    ``plain`` (member, no permission), ``other`` (administrator of org B).  A three-section checklist with one rule."""

    def with_role(email, name, perms):
        user = make_user(email=email, organization=org_a, role=None)
        assign(user, org_a, rbac.create_role(organization=org_a, name=name, permissions=perms))
        return user

    author = make_user(email="vs-author@example.com", organization=org_a, role=None)
    assign(author, org_a, "administrator")
    editor = with_role("vs-editor@example.com", "Editor", [P.CHECKLIST_VIEW, P.CHECKLIST_CREATE, P.CHECKLIST_UPDATE])
    publisher = with_role("vs-publisher@example.com", "Publisher", [P.CHECKLIST_VIEW, P.CHECKLIST_PUBLISH])
    reader = with_role("vs-reader@example.com", "Reader", [P.CHECKLIST_VIEW])
    plain = make_user(email="vs-plain@example.com", organization=org_a, role=None)
    other = make_user(email="vs-other@example.com", organization=org_b, role=None)
    assign(other, org_b, "administrator")

    template = services.create_checklist_template(
        organization=org_a, name="Restaurant Daily Safety Checklist", code="RDSC", actor=author,
        instructions="Walk the restaurant and answer every question.",
    )
    draft = template.versions.get()
    fire = services.create_section(draft, name="Fire Safety", actor=author)
    food = services.create_section(draft, name="Food Storage", actor=author)
    general = services.create_section(draft, name="General Operations", actor=author)
    services.create_item(fire, question="Is the fire exit accessible?", code="FIRE-EXIT", answer_type="yes_no", actor=author)
    services.create_item(food, question="Is the handwashing station operational?", code="HANDWASH", answer_type="yes_no_na", actor=author)
    services.create_item(food, question="Record refrigerator temperature.", code="FRIDGE-TEMP", answer_type="numeric",
                         config={"min": 0, "max": 10, "decimals": 1, "unit": "°C"}, actor=author)
    services.create_item(food, question="Condition of the storage area", code="CONDITION", answer_type="single_choice",
                         options=[("good", "Good"), ("fair", "Fair"), ("poor", "Poor")], actor=author)
    services.create_item(general, question="Describe any identified issue.", code="ISSUE", answer_type="long_text",
                         is_required=False, actor=author)
    services.create_rule(
        draft, name="Blocked fire exit", actor=author,
        condition={"source_item_code": "FIRE-EXIT", "comparison": "EQUALS", "value": "no"},
        actions=[act("SHOW_ITEM", "ISSUE"), act("REQUIRE_ITEM", "ISSUE")],
    )

    foreign = services.create_checklist_template(organization=org_b, name="Foreign", code="FX-1", actor=other)
    foreign_section = services.create_section(foreign.versions.get(), name="F", actor=other)
    services.create_item(foreign_section, question="Secret", code="SECRET", actor=other)
    services.publish_checklist_version(foreign.versions.get(), actor=other)

    world = {
        "author": author, "editor": editor, "publisher": publisher, "reader": reader, "plain": plain, "other": other,
        "template": template, "draft": draft, "foreign": foreign, "foreign_version": foreign.versions.get(),
    }
    if publish:
        world["v1"] = services.publish_checklist_version(draft, actor=author, change_summary="Initial version")
        services.activate_checklist_template(template, actor=author)
    return world


def item_of(version, code):
    from apps.checklists.models import ChecklistItem

    return ChecklistItem.objects.get(section__version=version, code=code)
