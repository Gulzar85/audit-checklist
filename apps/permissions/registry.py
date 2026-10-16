"""The permission registry: the single list of every permission the platform knows.

A permission code is ``<resource>.<action>`` (``location.update``).  Code never writes the
string: it imports the constant from :class:`P`, so a typo is an ``AttributeError`` at import
time rather than a silently-denied request.  The registry is the source of truth; the
``permissions.Permission`` table is a mirror that :func:`apps.permissions.services.sync_registry`
keeps up to date (``seed_permissions``) so roles can reference permissions with a real foreign
key and administrators can browse them.

``legacy`` names the Django model permission that granted the same capability before Phase 3.
It is honoured as an *organization-wide* grant while ``RBAC_LEGACY_MODEL_PERMISSIONS`` is on
(ADR 0043), which keeps pre-RBAC administrators working during the migration.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PermissionDef:
    code: str
    resource: str
    action: str
    label: str
    description: str = ""
    #: ``app_label.codename`` of the equivalent Django model permission (compatibility only).
    legacy: str | None = None
    #: Platform-level capability: meaningful only organization-wide (no location scoping).
    org_wide_only: bool = False


def _p(resource: str, action: str, label: str, description: str = "", *, legacy: str | None = None,
       org_wide_only: bool = False) -> PermissionDef:
    return PermissionDef(f"{resource}.{action}", resource, action, label, description, legacy, org_wide_only)


RESOURCE_LABELS: dict[str, str] = {
    "organization": "Organizations",
    "location": "Locations",
    "location_type": "Location types",
    "user": "Users",
    "membership": "Memberships",
    "role": "Roles",
    "permission": "Permissions",
    "theme": "Themes",
    "audit_log": "Audit log",
    "checklist": "Checklists",
    "assignment": "Assignments",
    "execution": "Executions",
    "finding": "Findings",
    "corrective_action": "Corrective actions",
    "report": "Reports",
    "dashboard": "Dashboards",
}

REGISTRY: tuple[PermissionDef, ...] = (
    _p("organization", "view", "View organizations", legacy="organizations.view_organization"),
    _p("organization", "update", "Edit organization details and branding", legacy="organizations.change_organization",
       org_wide_only=True),
    _p("location", "view", "View locations", legacy="locations.view_location"),
    _p("location", "create", "Create locations", legacy="locations.add_location"),
    _p("location", "update", "Edit locations", legacy="locations.change_location"),
    _p("location", "deactivate", "Deactivate and reactivate locations", legacy="locations.change_location"),
    _p("location", "move", "Move locations within the hierarchy", legacy="locations.change_location"),
    _p("location_type", "view", "View location types", legacy="locations.view_locationtype", org_wide_only=True),
    _p("location_type", "create", "Create location types", legacy="locations.add_locationtype", org_wide_only=True),
    _p("location_type", "update", "Edit location types", legacy="locations.change_locationtype", org_wide_only=True),
    _p("user", "view", "View users and their access", org_wide_only=True),
    _p("user", "update", "Edit users", org_wide_only=True),
    _p("membership", "view", "View organization memberships", org_wide_only=True),
    _p("membership", "manage", "Add, suspend and remove members", org_wide_only=True),
    _p("role", "view", "View roles", org_wide_only=True),
    _p("role", "create", "Create roles", org_wide_only=True),
    _p("role", "update", "Edit roles and their permissions", org_wide_only=True),
    _p("role", "deactivate", "Deactivate roles", org_wide_only=True),
    _p("role", "assign", "Assign roles and scopes to members", "Grant, change and remove a member's roles.",
       org_wide_only=True),
    _p("permission", "view", "View the permission catalogue", org_wide_only=True),
    _p("theme", "view", "View themes", legacy="themes.view_theme", org_wide_only=True),
    _p("theme", "create", "Create and import themes", legacy="themes.add_theme", org_wide_only=True),
    _p("theme", "update", "Edit themes", legacy="themes.change_theme", org_wide_only=True),
    _p("theme", "publish", "Publish themes", legacy="themes.publish_theme", org_wide_only=True),
    _p("theme", "activate", "Activate themes", legacy="themes.activate_theme", org_wide_only=True),
    _p("audit_log", "view", "View the audit log", org_wide_only=True),
    _p("organization", "create", "Create organizations", org_wide_only=True),
    _p("organization", "manage", "Manage organization settings and lifecycle", org_wide_only=True),
    _p("location", "manage", "Manage the location hierarchy and its types"),
    _p("user", "create", "Invite and create users", org_wide_only=True),
    _p("user", "delete", "Remove users from the organization", org_wide_only=True),
    _p("user", "manage", "Manage users, lifecycle and access", org_wide_only=True),
    _p("role", "delete", "Delete custom roles", org_wide_only=True),
    _p("role", "manage", "Manage roles and permission assignment", org_wide_only=True),
    # Reserved for later phases: the authorization contract exists before the modules do.
    # Checklist *definitions* belong to the organization (not to a location), so they are org-wide.
    _p("checklist", "view", "View checklist templates", legacy="checklists.view_checklisttemplate", org_wide_only=True),
    _p("checklist", "create", "Create and duplicate checklist templates", legacy="checklists.add_checklisttemplate", org_wide_only=True),
    _p("checklist", "update", "Edit checklist templates and their draft structure", legacy="checklists.change_checklisttemplate", org_wide_only=True),
    _p("checklist", "publish", "Publish checklist versions", org_wide_only=True),
    _p("checklist", "delete", "Delete unused draft checklist templates", legacy="checklists.delete_checklisttemplate", org_wide_only=True),
    _p("checklist", "manage", "Archive and restore checklist templates, manage categories", org_wide_only=True),
    # Assignments (Phase 8): location-scoped - a grant at a location covers assignments *at* that location (and its subtree).
    _p("assignment", "view", "View checklist assignments", "Assignments at the locations the role is scoped to."),
    _p("assignment", "create", "Create and edit draft assignments"),
    _p("assignment", "issue", "Issue draft assignments"),
    _p("assignment", "update", "Change an assignment's responsible user, due date and priority"),
    _p("assignment", "cancel", "Cancel assignments"),
    _p("assignment", "reopen", "Reopen cancelled assignments as drafts"),
    _p("assignment", "bulk_create", "Assign a checklist version to many locations at once"),
    _p("execution", "view", "View checklist executions"),
    _p("execution", "create", "Start checklist executions"),
    _p("execution", "update", "Continue checklist executions"),
    _p("execution", "submit", "Submit checklist executions"),
    _p("finding", "view", "View findings"),
    _p("finding", "create", "Raise findings"),
    _p("finding", "update", "Edit findings"),
    _p("finding", "close", "Close findings"),
    _p("corrective_action", "view", "View corrective actions"),
    _p("corrective_action", "create", "Create corrective actions"),
    _p("corrective_action", "assign", "Assign corrective actions"),
    _p("corrective_action", "verify", "Verify corrective actions"),
    _p("corrective_action", "close", "Close corrective actions"),
    _p("report", "view", "View reports"),
    _p("dashboard", "view", "View dashboards"),
)

BY_CODE: dict[str, PermissionDef] = {definition.code: definition for definition in REGISTRY}


class _Codes:
    """``P.LOCATION_UPDATE == "location.update"`` - built from the registry, typo-proof."""

    def __init__(self) -> None:
        for definition in REGISTRY:
            setattr(self, f"{definition.resource}_{definition.action}".upper(), definition.code)

    def __getattr__(self, name: str) -> str:  # pragma: no cover - only reached for a typo
        raise AttributeError(f"Unknown permission constant P.{name}")


P = _Codes()


def is_registered(code: str) -> bool:
    return code in BY_CODE


def codes() -> tuple[str, ...]:
    return tuple(BY_CODE)


# ---------------------------------------------------------------------------
# System roles (seeded by ``seed_roles``; protected from deletion/edit of identity)
# ---------------------------------------------------------------------------
def _all_except(*excluded: str) -> tuple[str, ...]:
    return tuple(code for code in BY_CODE if code not in excluded)


_VIEW_ALL = tuple(code for code in BY_CODE if code.endswith(".view") and code != P.AUDIT_LOG_VIEW)

#: ``slug -> (name, description, permission codes)``.
SYSTEM_ROLES: dict[str, tuple[str, str, tuple[str, ...]]] = {
    "super-administrator": (
        "Super Administrator",
        "Every permission. Reserved for the platform operators.",
        tuple(BY_CODE),
    ),
    "administrator": (
        "Administrator",
        "Runs an organization: users, roles, locations and branding.",
        _all_except(),
    ),
    "qa-manager": (
        "QA Manager",
        "Oversees quality across the organization; read access plus location upkeep.",
        (*_VIEW_ALL, P.LOCATION_CREATE, P.LOCATION_UPDATE, P.LOCATION_MOVE, P.AUDIT_LOG_VIEW,
         P.CHECKLIST_CREATE, P.CHECKLIST_UPDATE, P.CHECKLIST_PUBLISH, P.CHECKLIST_MANAGE, P.FINDING_CREATE, P.FINDING_UPDATE,
         P.ASSIGNMENT_CREATE, P.ASSIGNMENT_ISSUE, P.ASSIGNMENT_UPDATE, P.ASSIGNMENT_CANCEL, P.ASSIGNMENT_REOPEN, P.ASSIGNMENT_BULK_CREATE,
         P.FINDING_CLOSE, P.CORRECTIVE_ACTION_CREATE, P.CORRECTIVE_ACTION_ASSIGN, P.CORRECTIVE_ACTION_VERIFY),
    ),
    "auditor": (
        "Auditor",
        "Reads the organization structure the audits run against.",
        (P.ORGANIZATION_VIEW, P.LOCATION_VIEW, P.LOCATION_TYPE_VIEW, P.CHECKLIST_VIEW, P.ASSIGNMENT_VIEW, P.EXECUTION_VIEW,
         P.EXECUTION_CREATE, P.EXECUTION_UPDATE, P.EXECUTION_SUBMIT, P.FINDING_VIEW, P.FINDING_CREATE),
    ),
    "operations-consultant": (
        "Operations Consultant",
        "Advisory read access across the organization.",
        (P.ORGANIZATION_VIEW, P.LOCATION_VIEW, P.LOCATION_TYPE_VIEW, P.USER_VIEW, P.REPORT_VIEW,
         P.DASHBOARD_VIEW, P.FINDING_VIEW, P.EXECUTION_VIEW),
    ),
    "operations-manager": (
        "Operations Manager",
        "Manages locations across an area.",
        (P.ORGANIZATION_VIEW, P.LOCATION_VIEW, P.LOCATION_CREATE, P.LOCATION_UPDATE, P.LOCATION_MOVE,
         P.LOCATION_DEACTIVATE, P.LOCATION_TYPE_VIEW, P.USER_VIEW, P.CHECKLIST_VIEW, P.EXECUTION_VIEW,
         P.FINDING_VIEW, P.CORRECTIVE_ACTION_VIEW, P.CORRECTIVE_ACTION_ASSIGN, P.REPORT_VIEW, P.DASHBOARD_VIEW,
         P.ASSIGNMENT_VIEW, P.ASSIGNMENT_CREATE, P.ASSIGNMENT_ISSUE, P.ASSIGNMENT_UPDATE, P.ASSIGNMENT_CANCEL),
    ),
    "restaurant-manager": (
        "Restaurant Manager",
        "Runs a single restaurant; scope this role to that location.",
        (P.ORGANIZATION_VIEW, P.LOCATION_VIEW, P.LOCATION_UPDATE, P.CHECKLIST_VIEW, P.ASSIGNMENT_VIEW, P.EXECUTION_VIEW,
         P.EXECUTION_CREATE, P.EXECUTION_UPDATE, P.EXECUTION_SUBMIT, P.FINDING_VIEW, P.CORRECTIVE_ACTION_VIEW),
    ),
    "restaurant-user": (
        "Restaurant User",
        "Frontline staff; minimal read access to their own location.",
        (P.ORGANIZATION_VIEW, P.LOCATION_VIEW, P.ASSIGNMENT_VIEW, P.EXECUTION_VIEW, P.EXECUTION_CREATE, P.EXECUTION_UPDATE,
         P.EXECUTION_SUBMIT),
    ),
    "report-viewer": (
        "Report Viewer",
        "Read-only access for reporting.",
        (P.ORGANIZATION_VIEW, P.LOCATION_VIEW, P.REPORT_VIEW, P.DASHBOARD_VIEW),
    ),
}
