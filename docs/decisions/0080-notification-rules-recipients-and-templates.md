# 0080. Notification rules, recipients and templates

- **Status:** Accepted
- **Date:** 2026-10-27

## Decision
* Rules are data (`NotificationRule`): event type, strategies, channels, template, priority, policy, filters, precedence. **Several rules combine** per recipient: channels are the union and the lowest precedence number decides template, priority and policy - so a specific rule can add a channel without losing the default and an administrator can still override text.
* Recipients are **resolved on the server** through RBAC at the object's location and then filtered by the owning module's `can_view`; an object is never revealed to someone who could not open it. The actor is excluded by default; the pool is bounded.
* Templates allow only declared `{{ variable }}` substitution - no tags, filters or attribute access - rendered by a builtin-less Django `Engine`; HTML escapes values. Templates are versioned (draft/active/archived); a notification records the template code and version it used.
* Default rules are created lazily per organization, editable, disableable, never deleted.

## Consequences
Predictable, explainable behaviour; no template injection surface; an organization can change who hears about what without code.
