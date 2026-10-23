"""The controlled **business-action registry** (ADR 0071): the only operations a workflow transition can invoke.

Each handler belongs to one target type, is identified by a stable code, and *calls the module's own lifecycle service* with the acting
user - it never writes a status.  The domain service rechecks authorization, separation of duties and the record's current state, so a
workflow can neither exceed the actor's rights nor push a record into a state its lifecycle forbids.  If the service refuses, the whole
transition rolls back (the workflow does not claim a business operation that did not happen).

Adding a handler: write a function ``fn(actor, obj, comment) -> dict`` (the structured outcome) and ``register`` it for a target type.
Designers can only pick codes from this registry; arbitrary callables, dotted paths or code are not accepted anywhere.
"""

from __future__ import annotations

from collections.abc import Callable
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

_IN_WORKFLOW: ContextVar[bool] = ContextVar("workflow_transition_in_progress", default=False)


def in_workflow() -> bool:
    """True while the engine is executing a transition: domain guards use it to tell a governed call from a direct one."""
    return _IN_WORKFLOW.get()


@dataclass(frozen=True)
class Handler:
    code: str
    target_type: str
    label: str
    fn: Callable[[Any, Any, str], dict[str, Any]]
    #: The domain action this handler performs (used by the domain guards to refuse a *direct* call while a workflow governs it).
    domain_action: str = ""
    comment_hint: str = ""


_HANDLERS: dict[str, Handler] = {}


def register(handler: Handler) -> None:
    _HANDLERS[handler.code] = handler


def get(code: str) -> Handler | None:
    return _HANDLERS.get(code)


def for_target(target_type: str) -> list[Handler]:
    return [h for h in _HANDLERS.values() if h.target_type == target_type]


def describe_all() -> list[dict[str, Any]]:
    return [{"code": h.code, "target_type": h.target_type, "label": h.label, "domain_action": h.domain_action} for h in _HANDLERS.values()]


def run(handler: Handler, actor: Any, obj: Any, comment: str) -> dict[str, Any]:
    token = _IN_WORKFLOW.set(True)
    try:
        return handler.fn(actor, obj, comment)
    finally:
        _IN_WORKFLOW.reset(token)


# ---------------------------------------------------------------------------------------------------------------------------------
def _corrective_action(name: str):  # noqa: ANN202
    def fn(actor: Any, obj: Any, comment: str) -> dict[str, Any]:
        from apps.corrective_actions import services

        updated = services.transition(obj, name, actor=actor, reason=comment)
        return {"status": updated.status, "reference": updated.reference}

    return fn


def _finding(name: str):  # noqa: ANN202
    def fn(actor: Any, obj: Any, comment: str) -> dict[str, Any]:
        from apps.findings import services

        updated = services.transition(obj, name, actor=actor, reason=comment)
        return {"status": updated.status, "reference": updated.reference}

    return fn


def register_defaults() -> None:
    for name, label in (("issue", "Issue the action"), ("start", "Start work"), ("block", "Report a blocker"), ("resume", "Resume work"),
                        ("submit", "Submit for verification"), ("verify", "Record verification: approved"), ("reject", "Record verification: rejected"),
                        ("close", "Close the action"), ("cancel", "Cancel the action"), ("reopen", "Reopen the action")):
        register(Handler(f"corrective_action.{name}", "corrective_action", label, _corrective_action(name), domain_action=name))
    for name, label in (("acknowledge", "Acknowledge the finding"), ("start", "Start work on the finding"), ("resolve", "Resolve the finding"),
                        ("close", "Close the finding"), ("reopen", "Reopen the finding"), ("cancel", "Cancel the finding")):
        register(Handler(f"finding.{name}", "finding", label, _finding(name), domain_action=name))
