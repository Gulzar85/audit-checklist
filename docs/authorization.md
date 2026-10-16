# Authorization developer guide

Never test roles, `is_staff` or `is_superuser` in business code. Ask the service.

1. Add the permission to `apps/permissions/registry.py` (and system roles if relevant); run `seed_roles`.
2. Start every queryset from an authorized selector (`get_accessible_locations(user, P.X)` or a domain selector).
3. Write a policy in `apps/<app>/policies.py` calling `has_permission(user, P.X, organization=..., location=...)`.
4. Views: `PermissionRequiredMixin` / `ScopedAccessMixin` (`apps/permissions/mixins.py`) or `PolicyMixin`.
5. API: `HasApplicationPermission` + `ScopedObjectPermission` (`apps/api/permissions.py`) or policy hooks.
6. Writes go through a service; log with `log_activity`.
7. Templates: `{% load rbac %}{% can "location.update" location as ok %}` is a display hint only.
8. Test org isolation, 404 vs 403, scope and escalation.
9. 404 = outside what you may see; 403 = visible but not permitted.

Cache: grants are cached and invalidated by signals; see ADR 0044.
