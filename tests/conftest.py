"""Shared pytest fixtures."""

from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model

from apps.organizations.models import Organization, OrganizationMembership
from apps.permissions.models import Role, UserRole

User = get_user_model()


@pytest.fixture
def user(db) -> "User":
    return User.objects.create_user(
        email="auditor@example.com",
        password="Str0ng-Passw0rd!",
        display_name="Audrey Auditor",
        first_name="Audrey",
        last_name="Auditor",
    )


@pytest.fixture
def superuser(db) -> "User":
    return User.objects.create_superuser(
        email="admin@example.com",
        password="Adm1n-Passw0rd!",
        display_name="Ada Admin",
    )


@pytest.fixture
def organization(db) -> Organization:
    return Organization.objects.create(name="Acme Corp", slug="acme")


@pytest.fixture
def membership(db, user, organization) -> OrganizationMembership:
    return OrganizationMembership.objects.create(
        user=user, organization=organization, is_default=True
    )


@pytest.fixture
def auditor_role(db) -> Role:
    return Role.objects.create(name="Auditor", slug="auditor")


@pytest.fixture
def granted_role(db, user, auditor_role) -> UserRole:
    return UserRole.objects.create(user=user, role=auditor_role)


@pytest.fixture
def authenticated_client(client, user):
    client.force_login(user)
    return client
