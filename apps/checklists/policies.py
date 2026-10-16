"""Authorization policy for checklist definitions (Phase 3 RBAC; ADR 0042/0043).

Checklist *definitions* belong to an organization, not to a location, so every check is an
organization-level permission (``checklist.*`` held organization-wide).  Nothing here names a role.

====================  ==========================================  ==================================
Action                Permission                                  Extra rule
====================  ==========================================  ==================================
view                  ``checklist.view``                          -
create / duplicate    ``checklist.create``                        -
update (metadata,     ``checklist.update``                        structure: version must be a draft
builder)
create / discard a    ``checklist.update``                        one draft per template; archived: no
draft
validate a draft      ``checklist.update`` or ``checklist.publish`` draft versions only
publish a version     ``checklist.publish``                       -
view / compare /      ``checklist.view``                          read-only, any version
preview / history
archive / restore     ``checklist.manage``                        -
delete                ``checklist.delete``                        only an unpublished draft template
====================  ==========================================  ==================================

The web views, the API and imports call these same functions.  Version, section, item, option and rule
policies exist as separate entry points so later modules (and tests) can depend on the specific
question being asked, but they all reduce to the template's organization.
"""

from __future__ import annotations

from typing import Any

from apps.core.policies import is_active_user
from apps.permissions.authorization import has_permission, has_permission_anywhere
from apps.permissions.registry import P

from .constants import TemplateStatus, VersionStatus


def _org(obj: Any) -> Any:
    return getattr(obj, "organization_id", None) or getattr(obj, "pk", obj)


def _can(user: Any, code: str, organization: Any) -> bool:
    return is_active_user(user) and has_permission(user, code, organization=organization)


# --- Templates ----------------------------------------------------------------------------
def can_view_template(user: Any, template: Any) -> bool:
    return _can(user, P.CHECKLIST_VIEW, template.organization_id)


def can_create_template(user: Any, organization: Any = None) -> bool:
    """``organization=None``: could the user create a template *somewhere* (to offer the button)?"""
    if organization is None:
        return has_permission_anywhere(user, P.CHECKLIST_CREATE)
    return _can(user, P.CHECKLIST_CREATE, _org(organization))


def can_duplicate_template(user: Any, template: Any) -> bool:
    return can_view_template(user, template) and _can(user, P.CHECKLIST_CREATE, template.organization_id)


def can_update_template(user: Any, template: Any) -> bool:
    return _can(user, P.CHECKLIST_UPDATE, template.organization_id)


def can_manage_template(user: Any, template: Any) -> bool:
    """Archive and restore."""
    return _can(user, P.CHECKLIST_MANAGE, template.organization_id)


def can_delete_template(user: Any, template: Any) -> bool:
    if template.status != TemplateStatus.DRAFT:
        return False
    return _can(user, P.CHECKLIST_DELETE, template.organization_id)


def can_manage_categories(user: Any, organization: Any) -> bool:
    return _can(user, P.CHECKLIST_MANAGE, _org(organization))


# --- Versions ------------------------------------------------------------------------------
def can_view_version(user: Any, version: Any) -> bool:
    return can_view_template(user, version.template)


def can_create_version(user: Any, template: Any) -> bool:
    return can_update_template(user, template)


def can_publish_template(user: Any, template: Any) -> bool:
    """May the user publish this checklist's drafts at all (status-independent, so a repeated request is a conflict, not a 403)?"""
    return _can(user, P.CHECKLIST_PUBLISH, template.organization_id)


def can_publish_version(user: Any, version: Any) -> bool:
    return version.status == VersionStatus.DRAFT and can_publish_template(user, version.template)


def can_edit_version(user: Any, version: Any) -> bool:
    """Edit the *structure*: requires update rights and a draft version of a live template."""
    return (
        version.status == VersionStatus.DRAFT
        and version.template.status != TemplateStatus.ARCHIVED
        and can_update_template(user, version.template)
    )


def can_validate_version(user: Any, version: Any) -> bool:
    """Run the publication validation of a draft (authors, and publishers who review before publishing)."""
    return version.status == VersionStatus.DRAFT and (can_edit_version(user, version) or can_publish_version(user, version))


def can_discard_version(user: Any, version: Any) -> bool:
    """Delete a draft nobody has relied upon (never a published or superseded version)."""
    return can_edit_version(user, version)


def can_compare_versions(user: Any, template: Any) -> bool:
    """Compare, preview and read the history of any version: viewing the checklist is enough (history is read-only)."""
    return can_view_template(user, template)


# --- Sections, items, options, rules: all follow their version --------------------------------
def can_view_section(user: Any, section: Any) -> bool:
    return can_view_version(user, section.version)


def can_edit_section(user: Any, section: Any) -> bool:
    return can_edit_version(user, section.version)


def can_view_item(user: Any, item: Any) -> bool:
    return can_view_section(user, item.section)


def can_edit_item(user: Any, item: Any) -> bool:
    return can_edit_section(user, item.section)


def can_edit_option(user: Any, option: Any) -> bool:
    return can_edit_item(user, option.item)


def can_edit_rule(user: Any, rule: Any) -> bool:
    return can_edit_version(user, rule.version)
