"""Custom user model.

Designed from day one to support Microsoft Entra ID as the primary identity
provider: an Entra object id is stored, email is the natural key, and local
passwords are an administrative fallback only.  Microsoft passwords are never
stored.

Lifecycle is modelled with a single authoritative ``status`` field
(:class:`~apps.accounts.constants.UserStatus`).  ``is_active`` is kept as a
*stored mirror* of ``status == ACTIVE`` so that Django's authentication gate
(``ModelBackend.user_can_authenticate``) and every existing ``is_active`` query
keep working, while administrators talk about meaningful states (pending,
active, suspended, inactive).  A database check constraint forbids the two from
disagreeing.
"""

from __future__ import annotations

import uuid

from django.contrib.auth.models import AbstractBaseUser, PermissionsMixin
from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from simple_history.models import HistoricalRecords

from .constants import UserStatus
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
    entra_tenant_id = models.CharField(
        _("Microsoft Entra tenant id"),
        max_length=64,
        blank=True,
        db_index=True,
        help_text=_(
            "Directory (tenant) the identity belongs to. Configuration-driven, never hard-coded."
        ),
    )
    display_name = models.CharField(max_length=255, blank=True)
    first_name = models.CharField(max_length=150, blank=True)
    last_name = models.CharField(max_length=150, blank=True)
    job_title = models.CharField(max_length=150, blank=True)
    phone_number = models.CharField(max_length=40, blank=True)
    avatar = models.ImageField(upload_to="avatars/", blank=True, null=True)

    status = models.CharField(
        _("status"),
        max_length=16,
        choices=UserStatus.choices,
        default=UserStatus.ACTIVE,
        db_index=True,
        help_text=_(
            "Authoritative lifecycle state. 'is_active' is derived from this and "
            "kept in lock-step automatically."
        ),
    )
    is_active = models.BooleanField(
        _("active"),
        default=True,
        db_index=True,
        help_text=_("Stored mirror of status == active; Django's authentication gate."),
    )
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
        constraints = [
            # ``is_active`` must equal ``status == 'active'`` - the two fields can
            # never disagree, whatever path wrote the row.
            models.CheckConstraint(
                condition=(
                    models.Q(status=UserStatus.ACTIVE, is_active=True)
                    | (~models.Q(status=UserStatus.ACTIVE) & models.Q(is_active=False))
                ),
                name="user_status_matches_is_active",
            ),
        ]

    def save(self, *args, **kwargs):  # noqa: ANN002, ANN003
        # ``status`` is authoritative; derive the authentication gate from it.
        self.is_active = self.status == UserStatus.ACTIVE
        update_fields = kwargs.get("update_fields")
        if update_fields is not None:
            update_fields = set(update_fields)
            update_fields.add("is_active")
            kwargs["update_fields"] = update_fields
        super().save(*args, **kwargs)

    def __str__(self) -> str:
        return self.display_name or self.email

    def get_full_name(self) -> str:
        return self.display_name or f"{self.first_name} {self.last_name}".strip()

    def get_short_name(self) -> str:
        return self.first_name or self.display_name or self.email

    # --- Derived lifecycle state -----------------------------------------
    @property
    def is_pending(self) -> bool:
        return self.status == UserStatus.PENDING

    @property
    def is_suspended(self) -> bool:
        return self.status == UserStatus.SUSPENDED

    @property
    def is_inactive(self) -> bool:
        return self.status == UserStatus.INACTIVE

    @property
    def is_entra_linked(self) -> bool:
        return bool(self.entra_object_id)

    @property
    def can_sign_in(self) -> bool:
        """Whether this account may currently authenticate and use the app."""
        return self.status == UserStatus.ACTIVE

    @property
    def initials(self) -> str:
        source = self.get_full_name() or self.email
        parts = [part for part in source.replace("@", " ").split() if part]
        return "".join(part[0].upper() for part in parts[:2])
