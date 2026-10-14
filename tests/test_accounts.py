"""The custom user model and its manager."""

from __future__ import annotations

import pytest
from django.contrib.auth import authenticate, get_user_model

User = get_user_model()


@pytest.mark.django_db
def test_create_user_sets_password_and_defaults():
    user = User.objects.create_user(email="Person@Example.com", password="Str0ng-Passw0rd!")
    assert user.email == "Person@example.com"  # domain lower-cased
    assert user.check_password("Str0ng-Passw0rd!")
    assert user.is_active
    assert not user.is_staff
    assert not user.is_superuser
    assert user.pk is not None


@pytest.mark.django_db
def test_create_user_without_password_gets_unusable_password():
    user = User.objects.create_user(email="entra@example.com")
    assert not user.has_usable_password()


@pytest.mark.django_db
def test_create_superuser_flags():
    user = User.objects.create_superuser(email="root@example.com", password="Adm1n-Passw0rd!")
    assert user.is_staff and user.is_superuser


@pytest.mark.django_db
def test_get_by_natural_key_is_case_insensitive():
    User.objects.create_user(email="Mixed@Example.com", password="Str0ng-Passw0rd!")
    fetched = User.objects.get_by_natural_key("mixed@example.com")
    assert fetched.email == "Mixed@example.com"


@pytest.mark.django_db
def test_authentication_by_email(user):
    assert authenticate(username="auditor@example.com", password="Str0ng-Passw0rd!") == user
    assert authenticate(username="auditor@example.com", password="wrong") is None


@pytest.mark.django_db
def test_display_helpers(user):
    assert user.get_full_name() == "Audrey Auditor"
    assert user.get_short_name() == "Audrey"
    assert user.initials == "AA"
    assert str(user) == "Audrey Auditor"


@pytest.mark.django_db
def test_entra_object_id_is_unique_and_optional():
    User.objects.create_user(email="a@example.com", entra_object_id="entra-1")
    with pytest.raises(Exception):
        User.objects.create_user(email="b@example.com", entra_object_id="entra-1")
