# 0071. Target and business-action registries, transactional integration and domain governance

- **Status:** Accepted
- **Date:** 2026-10-23

## Decision
* **Targets are a controlled registry, not a generic foreign key.** A client names a registered `target_type` and an id; trusted code resolves the record through the owning module's authorized selector, reports organization / location / owner / creator and the attributes conditions may use. A database trigger keeps instance, definition, version, state and location consistent.
* **Business actions are a registry of handlers** per target type; each calls the module's lifecycle service as the acting user. Designers pick codes from the registry; nothing else is executable. The domain service rechecks authorization, separation of duties and state.
* **One transaction**: the workflow transition and the domain operation commit or roll back together; a domain refusal means the workflow never records a business success. No external side effects exist inside the transaction in Phase 14; later ones must use after-commit signals / a reliable-processing pattern (not built now).
* **Duplicate active workflows**: at most one active instance per (record, definition) (database constraint); different definitions may run beside each other.
* **Governance guard:** while a workflow governs a domain decision, direct calls to that decision are refused (`governed_by_workflow`) through guards registered with Phases 12 and 13 (they do not import the workflow app); the engine's own call passes via a context flag. This prevents drift and bypass without moving domain rules into the workflow.
* **Auto-start** is driven by after-commit domain signals; problems are audited, never raised into the domain flow.

## Consequences
Findings and corrective actions keep their lifecycle logic; the workflow can only invoke it. Adding a target or a handler is a registry entry.
