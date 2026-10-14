"""Custom user model.

Designed from day one to support Microsoft Entra ID as the primary identity
provider: an Entra object id is stored, email is the natural key, and local
passwords are an administrative fallback only.  Microsoft passwords are never
stored.
"""

from __future__ import annotations

import uuid

from django.contrib.auth.models import AbstractBaseUser, PermissionsMixin
from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from simple_history.models import HistoricalRecords

from .managers import UserManager


class User(AbstractBaseUser, PermissionsMixin):
    """The platform user."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    email = models.EmailField(_("email address"), unique=True)
    entra_object_id = models.CharField(
        _("Microsoft Entra object id"),
        max_length=64,
        unique=True,
        null=True,
        blank=True,
        help_text=_("Immutable identifier from Microsoft Entra ID."),
    )
    display_name = models.CharField(max_length=255, blank=True)
    first_name = models.CharField(max_length=150, blank=True)
    last_name = models.CharField(max_length=150, blank=True)
    job_title = models.CharField(max_length=150, blank=True)
    phone_number = models.CharField(max_length=40, blank=True)
    avatar = models.ImageField(upload_to="avatars/", blank=True, null=True)

    is_active = models.BooleanField(default=True, db_index=True)
    is_staff = models.BooleanField(default=False)
    date_joined = models.DateTimeField(default=timezone.now, editable=False)

    organizations = models.ManyToManyField(
        "organizations.Organization",
        through="organizations.OrganizationMembership",
        related_name="members",
        blank=True,
        verbose_name=_("organizations"),
    )

    history = HistoricalRecords()

    objects = UserManager()

    USERNAME_FIELD = "email"
    EMAIL_FIELD = "email"
    REQUIRED_FIELDS: list[str] = []

    class Meta:
        verbose_name = _("user")
        verbose_name_plural = _("users")
        ordering = ("email",)

    def __str__(self) -> str:
        return self.display_name or self.email

    def get_full_name(self) -> str:
        return self.display_name or f"{self.first_name} {self.last_name}".strip()

    def get_short_name(self) -> str:
        return self.first_name or self.display_name or self.email

    @property
    def initials(self) -> str:
        source = self.get_full_name() or self.email
        parts = [part for part in source.replace("@", " ").split() if part]
        return "".join(part[0].upper() for part in parts[:2])
