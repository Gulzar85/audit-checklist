"""The controlled task registry (Phase 18): the only things a schedule or job can ever run.

A task is a server-side definition - a stable code, a strict payload schema, a scope, who may trigger it, a retry policy and a handler that calls an existing,
reviewed service.  A job stores a *code*, never a function path, and the runner looks the code up here; an unknown code is refused at creation and again at
execution.  Payloads are validated against the schema (unknown keys are errors) and never carry an organization or a user: those come from the job row.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from apps.permissions.registry import P

from . import constants as k
from .errors import JobError

SCOPE_GLOBAL = "global"  # runs for the whole platform (superusers only)
SCOPE_ORGANIZATION = "organization"  # always for one organization
SCOPE_BOTH = "both"  # one organization, or (superusers) every organization


@dataclass(frozen=True)
class Field:
    """One payload field: an integer in ``[minimum, maximum]``, a boolean, or a string from ``choices``."""

    kind: str  # "int" | "bool" | "choice" | "uuid"
    default: Any
    minimum: int = 0
    maximum: int = 0
    choices: tuple[str, ...] = ()
    help: str = ""


@dataclass
class Context:
    """What a handler receives.  ``organization`` is ``None`` for a platform-wide job; the handler passes it to the service it calls and never reads an id from the payload."""

    job_id: str
    organization: Any
    payload: dict[str, Any]
    correlation_id: str
    deadline: Any = None


@dataclass(frozen=True)
class Task:
    code: str
    name: str
    description: str
    scope: str
    handler: Callable[[Context], dict[str, Any]]
    fields: dict[str, Field] = field(default_factory=dict)
    permission: str = P.JOBS_RUN
    schedulable: bool = True
    manual: bool = True
    max_attempts: int = 3
    base_delay_seconds: int = 60
    max_delay_seconds: int = 3600
    soft_limit_seconds: int = 300
    idempotent: bool = True  # every registered handler must be safe to run again (at-least-once)


REGISTRY: dict[str, Task] = {}


def register(task: Task) -> Task:
    if task.code in REGISTRY:
        raise RuntimeError(f"task {task.code!r} is already registered")
    REGISTRY[task.code] = task
    return task


def get(code: Any) -> Task | None:
    return REGISTRY.get(code) if isinstance(code, str) else None


def require(code: Any) -> Task:
    task = get(code)
    if task is None:
        raise JobError("That task type is not available.", "unknown_task")
    return task


def clean_payload(task: Task, raw: Any) -> dict[str, Any]:
    """Validate ``raw`` against the task's schema; returns the normalized payload (defaults filled in).  Unknown keys, wrong types and out-of-range values are refused."""
    if raw in (None, ""):
        raw = {}
    if not isinstance(raw, dict):
        raise JobError("The payload must be an object.", "invalid_payload")
    unknown = sorted(set(raw) - set(task.fields))
    if unknown:
        raise JobError(f"Unknown payload field(s): {', '.join(map(str, unknown))[:80]}.", "invalid_payload")
    out: dict[str, Any] = {}
    for name, spec in task.fields.items():
        value = raw.get(name, spec.default)
        if spec.kind == "bool":
            if not isinstance(value, bool):
                raise JobError(f"{name} must be true or false.", "invalid_payload")
        elif spec.kind == "int":
            if isinstance(value, bool) or not isinstance(value, int) or not spec.minimum <= value <= spec.maximum:
                raise JobError(f"{name} must be a whole number from {spec.minimum} to {spec.maximum}.", "invalid_payload")
        elif spec.kind == "uuid":
            from apps.core.utils import parse_uuid

            parsed = parse_uuid(value) if isinstance(value, str) else None
            if parsed is None:
                raise JobError(f"{name} must be an identifier.", "invalid_payload")
            value = str(parsed)
        elif spec.kind == "choice":
            if value not in spec.choices:
                raise JobError(f"{name} must be one of: {', '.join(spec.choices)}.", "invalid_payload")
        else:  # pragma: no cover - a registry definition error
            raise JobError("Unsupported payload field.", "invalid_payload")
        out[name] = value
    return out


def retry_limits(task: Task, requested: int | None) -> int:
    ceiling = min(task.max_attempts, k.MAX_ATTEMPTS_LIMIT)
    return ceiling if requested is None else max(1, min(int(requested), ceiling))


def describe(task: Task) -> dict[str, Any]:
    return {"code": task.code, "name": task.name, "description": task.description, "scope": task.scope, "schedulable": task.schedulable, "manual": task.manual,
            "max_attempts": task.max_attempts, "permission": task.permission,
            "fields": {n: {"kind": f.kind, "default": f.default, "minimum": f.minimum, "maximum": f.maximum, "choices": list(f.choices), "help": f.help} for n, f in task.fields.items()}}
