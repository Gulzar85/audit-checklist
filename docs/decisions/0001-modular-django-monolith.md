# 0001. Modular Django monolith

- **Status:** Accepted
- **Date:** 2026-10-14

## Context

The platform must serve many inspection domains (QA, operations, safety,
restaurant, compliance) from one codebase, by a small team, with an emphasis on
correctness and speed of delivery.

## Decision

Build a **modular monolith**: a single Django project composed of cohesive apps
with explicit responsibilities, a service layer for writes, selectors for reads,
and a single authorization boundary. Distribute as one deployable unit.

## Consequences

- Simple operations and transactions; no distributed-systems failure modes.
- Clear module boundaries make later extraction possible **if ever needed**.
- Discipline is required to keep apps decoupled; cross-app access goes through
  services/selectors, not direct model spelunking.
