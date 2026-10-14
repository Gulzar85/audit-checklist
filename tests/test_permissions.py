"""RBAC foundation and the authorization boundary."""

from __future__ import annotations

from datetime import timedelta

import pytest
from django.contrib.auth.models import AnonymousUser
from django.utils import timezone

from apps.permissions.authorization import (
    can_execute_checklist,
    can_manage_finding,
    can_view_checklist,
    can_verify_action,
    has_role,
)
from apps.permissions.models import Role, UserRole
from apps.permissions.selectors import active_role_slugs_for_user, roles_for_organization


@pytest.fixture
def anonymous():
    return AnonymousUser()


def test_anonymous_user_is_denied(anonymous):
    assert can_view_checklist(anonymous, object()) is False
    assert can_execute_checklist(anonymous, object()) is False
    assert can_manage_finding(anonymous, object()) is False
    assert can_verify_action(anonymous, object()) is False
    assert has_role(anonymous, "auditor") is False


@pytest.mark.django_db
def test_active_user_passes_baseline(user):
    assert can_view_checklist(user, object()) is True
    assert can_execute_checklist(user, object()) is True
    assert can_manage_finding(user, object()) is True
    assert can_verify_action(user, object()) is True


@pytest.mark.django_db
def test_inactive_user_is_denied(db):
    from django.contrib.auth import get_user_model

    inactive = get_user_model().objects.create_user(
        email="inactive@example.com", password="Str0ng-Passw0rd!", is_active=False
    )
    assert can_view_checklist(inactive, object()) is False


@pytest.mark.django_db
def test_has_role_reflects_assignments(user, granted_role):
    assert has_role(user, "auditor") is True
    assert has_role(user, "supervisor") is False


@pytest.mark.django_db
def test_expired_role_is_excluded(user, auditor_role):
    UserRole.objects.create(
        user=user,
        role=auditor_role,
        expires_at=timezone.now() - timedelta(days=1),
    )
    assert active_role_slugs_for_user(user) == set()
    assert has_role(user, "auditor") is False


@pytest.mark.django_db
def test_inactive_role_is_excluded(user, auditor_role):
    auditor_role.is_active = False
    auditor_role.save(update_fields=["is_active"])
    UserRole.objects.create(user=user, role=auditor_role)
    assert has_role(user, "auditor") is False


@pytest.mark.django_db
def test_roles_are_data_not_code(db):
    slug = "district-manager"
    role = Role.objects.create(name="District Manager", slug=slug)
    assert role.organization is None
    assert str(role).startswith("District Manager")


@pytest.mark.django_db
def test_roles_for_organization_includes_platform_roles(organization):
    Role.objects.create(name="Auditor", slug="auditor")
    Role.objects.create(name="Owner", slug="owner", organization=organization)
    slugs = set(roles_for_organization(organization.pk).values_list("slug", flat=True))
    assert slugs == {"auditor", "owner"}
