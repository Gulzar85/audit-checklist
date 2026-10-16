# Access control matrix

| Action | Permission | Scope applied at |
|---|---|---|
| View location | location.view | the location |
| Create location | location.create | the parent (root: organization-wide) |
| Edit / deactivate | location.update / location.deactivate | the location |
| Move | location.move | source and destination |
| Edit organization | organization.update | organization |
| Location types | location_type.* | organization |
| List users | user.view | organization |
| Manage membership status | membership.manage | organization (never yourself) |
| Roles | role.view/create/update/deactivate | organization; may not exceed your own permissions |
| Assign role | role.assign | target scope; role permissions must be held at that scope |
| View checklist templates | checklist.view | organization |
| Create / duplicate template | checklist.create | organization |
| Edit template and draft structure | checklist.update | organization; version must be a draft |
| Publish a version | checklist.publish | organization |
| Archive / restore | checklist.manage | organization |
| Delete an unpublished draft | checklist.delete | organization |
| Themes / audit log | theme.*, audit_log.view | organization |

Platform operations (create/suspend organizations, user lifecycle) remain with platform staff.
