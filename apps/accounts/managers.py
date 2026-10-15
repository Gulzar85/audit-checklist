"""User manager for the custom user model."""

from __future__ import annotations

from typing import Any

from django.contrib.auth.models import UserManager as BaseUserManager

from .constants import UserStatus


class UserManager(BaseUserManager):
    """Manager for :class:`~apps.accounts.models.User`.

    Email is the natural identifier; a password is optional because enterprise
    authentication is delegated to Microsoft Entra ID.  Local passwords exist
    only as an administrative fallback.
    """

    use_in_migrations = True

    def _create_user(self, email: str, password: str | None, **extra_fields: Any):
        if not email:
            raise ValueError("The email address must be set.")
        email = self.normalize_email(email)
        # ``status`` is authoritative and ``is_active`` is derived from it in
        # User.save().  Preserve the historical ``is_active=False`` calling
        # convention (fixtures, admin, tests) by translating it to ``inactive``.
        is_active = extra_fields.pop("is_active", True)
        if not is_active and "status" not in extra_fields:
            extra_fields["status"] = UserStatus.INACTIVE
        user = self.model(email=email, **extra_fields)
        if password:
            user.set_password(password)
        else:
            user.set_unusable_password()
        user.save(using=self._db)
        return user

    def create_user(self, email: str, password: str | None = None, **extra_fields: Any):
        extra_fields.setdefault("is_staff", False)
        extra_fields.setdefault("is_superuser", False)
        return self._create_user(email, password, **extra_fields)

    def create_superuser(self, email: str, password: str | None = None, **extra_fields: Any):
        extra_fields.setdefault("is_staff", True)
        extra_fields.setdefault("is_superuser", True)
        if extra_fields.get("is_staff") is not True:
            raise ValueError("Superuser must have is_staff=True.")
        if extra_fields.get("is_superuser") is not True:
            raise ValueError("Superuser must have is_superuser=True.")
        return self._create_user(email, password, **extra_fields)

    def get_by_natural_key(self, username: str):
        return self.get(**{f"{self.model.USERNAME_FIELD}__iexact": username})
