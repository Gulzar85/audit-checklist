# 0008. Versioned API with a response envelope

- **Status:** Accepted
- **Date:** 2026-10-14

## Context

Clients (PWA, integrations, future mobile) need a predictable, evolvable API
contract with consistent success and error shapes.

## Decision

Mount a **versioned** API at `/api/v1/` using DRF `NamespaceVersioning`, and
apply a central **response envelope** (`EnvelopeJSONRenderer`) plus a single
`api_exception_handler`:

- success: `{"data": <payload>, "meta": {...}}`
- error: `{"error": {"code", "message", "details"}}`

## Consequences

- Clients parse one contract; views return plain payloads.
- Error codes are stable, machine-readable identifiers (`ErrorCode`).
- Non-breaking evolution is possible by introducing `v2` alongside `v1`.
