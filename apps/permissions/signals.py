"""Cache invalidation: any change that can alter what someone may do drops the cached grants.

Wired through signals (not only services) so edits made in the admin, by a management command
or by a test fixture invalidate exactly like a service call.  Bulk ``QuerySet.update()`` skips
signals by design in Django - code that uses it on these tables must call
:func:`apps.permissions.authorization.invalidate_all` itself.
"""

from __future__ import annotations

from django.db.models.signals import m2m_changed, post_delete, post_save
from django.dispatch import receiver

from . import authorization
from .models import MembershipRole, Permission, Role, RolePermission


def _user_of_membership(instance) -> None:  # noqa: ANN001
    authorization.invalidate_user(instance.user_id)


def connect() -> None:
    from django.contrib.auth import get_user_model

    from apps.organizations.models import Organization, OrganizationMembership

    user_model = get_user_model()

    @receiver((post_save, post_delete), sender=OrganizationMembership, weak=False, dispatch_uid="rbac_membership")
    def _membership(sender, instance, **kwargs):  # noqa: ANN001, ANN003, ARG001
        _user_of_membership(instance)

    @receiver((post_save, post_delete), sender=MembershipRole, weak=False, dispatch_uid="rbac_membership_role")
    def _assignment(sender, instance, **kwargs):  # noqa: ANN001, ANN003, ARG001
        authorization.invalidate_user(instance.membership.user_id)

    @receiver((post_save, post_delete), sender=RolePermission, weak=False, dispatch_uid="rbac_role_permission")
    @receiver((post_save, post_delete), sender=Role, weak=False, dispatch_uid="rbac_role")
    @receiver((post_save, post_delete), sender=Permission, weak=False, dispatch_uid="rbac_permission")
    @receiver(post_save, sender=Organization, weak=False, dispatch_uid="rbac_organization")
    def _global(sender, **kwargs):  # noqa: ANN001, ANN003, ARG001
        authorization.invalidate_all()

    @receiver(post_save, sender=user_model, weak=False, dispatch_uid="rbac_user")
    def _user(sender, instance, **kwargs):  # noqa: ANN001, ANN003, ARG001
        authorization.invalidate_user(instance.pk)

    @receiver(m2m_changed, sender=user_model.user_permissions.through, weak=False, dispatch_uid="rbac_user_perms")
    @receiver(m2m_changed, sender=user_model.groups.through, weak=False, dispatch_uid="rbac_user_groups")
    def _m2m(sender, instance, **kwargs):  # noqa: ANN001, ANN003, ARG001
        if isinstance(instance, user_model):
            authorization.invalidate_user(instance.pk)
        else:  # a permission/group edited from the other side
            authorization.invalidate_all()
