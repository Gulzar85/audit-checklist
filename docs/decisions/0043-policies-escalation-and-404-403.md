# 0043. Policies, no escalation, and the 404 / 403 rule

- **Status:** Accepted
- **Date:** 2026-10-16

## Decision
* **404** when the object is outside what the user may see (other tenant, not a member, outside location scope);
  **403** when visible but the permission is missing. Selectors authorize *before* search, filter and pagination.
* No privilege escalation: a role (create or edit) may only carry permissions the actor holds; assigning a role
  needs `role.assign` *at the target scope* and every permission of the role at that scope; nobody changes their
  own access or membership status; only a superuser assigns the Super Administrator role.
* Creating a location needs `location.create` at the **parent** (root: organization-wide). Moving needs rights at
  the source and the destination. Ancestors above a user's scope are never named in the UI or API (the topmost
  visible location is the root of their tree; its `parent` is reported as null).
* Hiding a menu item or button is presentation only; the server re-checks on every request.

## Consequences
The 403 page ("Access restricted") explains what to do next. Policies live in `apps/*/policies.py`.
