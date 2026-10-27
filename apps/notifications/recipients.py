"""Server-side recipient resolution over a fixed strategy registry.

A rule names *strategies*, never users' addresses.  Each strategy resolves to active members of the event's organization; the permission / role / user strategies reuse
the workflow engine's assignee-policy matcher (``apps.workflows.assignment``: RBAC permission *at the object's location*, role and location scope, active membership), so a
global role never sends business data to people outside the scope.  Whatever a strategy returns is then filtered by the **owning module's own view policy**
(``registry.Source.can_view``): a person who could not open the object is never told about it - receiving a notification is not access, and not receiving one costs nothing.
The actor is excluded when the rule says so, recipients are deduplicated, and the pool is bounded.
"""

from __future__ import annotations

from typing import Any

from django.contrib.auth import get_user_model

from apps.core.utils import parse_uuid
from apps.workflows import assignment as policy_matcher

from . import constants as k
from .registry import Source

User = get_user_model()


def as_policy(strategy: dict[str, Any]) -> dict[str, Any] | None:
    """The workflow assignee-policy shape of a user / role / permission strategy (``None`` for the object-relative strategies)."""
    kind = strategy.get("type")
    if kind == k.Strategy.USER:
        return {"type": "user", "users": [strategy.get("user")]}
    if kind == k.Strategy.ROLE:
        return {"type": "role", "role": strategy.get("role")}
    if kind == k.Strategy.PERMISSION:
        return {"type": "permission", "permission": strategy.get("permission")}
    return None


def clean_strategies(raw: Any, organization: Any) -> tuple[list[dict[str, Any]], list[str]]:
    """Validate a rule's strategies.  Returns ``(normalized, problems)``."""
    if not isinstance(raw, list) or not raw or len(raw) > k.MAX_STRATEGIES:
        return [], [f"recipients: a list of 1 to {k.MAX_STRATEGIES} strategies is required."]
    out: list[dict[str, Any]] = []
    problems: list[str] = []
    for i, item in enumerate(raw, 1):
        where = f"recipients[{i}]"
        if not isinstance(item, dict) or str(item.get("type", "")) not in k.Strategy.values:
            problems.append(f"{where}: type must be one of {', '.join(k.Strategy.values)}.")
            continue
        kind = item["type"]
        allowed = {"type"} | ({"user"} if kind == k.Strategy.USER else {"role"} if kind == k.Strategy.ROLE else {"permission"} if kind == k.Strategy.PERMISSION else set())
        if set(item) - allowed:
            problems.append(f"{where}: unexpected keys: {', '.join(sorted(set(item) - allowed))}.")
            continue
        policy = as_policy(item)
        if policy is not None:
            if kind == k.Strategy.USER and parse_uuid(item.get("user")) is None:
                problems.append(f"{where}: choose the user (a valid user id).")
                continue
            cleaned, found = policy_matcher.clean_policy(policy, organization)
            if found or cleaned is None:
                problems.extend(f"{where}: {p}" for p in (found or ["invalid."]))
                continue
            normalized = {"type": kind, **({"user": str(item["user"])} if kind == k.Strategy.USER else {}), **({"role": cleaned["role"]} if kind == k.Strategy.ROLE else {}),
                          **({"permission": cleaned["permission"]} if kind == k.Strategy.PERMISSION else {})}
            out.append(normalized)
        else:
            out.append({"type": kind})
    return (out if not problems else []), problems


def _by_id(pk: Any) -> list[Any]:
    return list(User.objects.filter(pk=pk, is_active=True)) if pk else []


def _members_ok(users: list[Any], organization: Any) -> list[Any]:
    from apps.organizations.models import OrganizationMembership

    ids = set(OrganizationMembership.objects.filter(organization=organization, is_active=True, user__in=users).values_list("user_id", flat=True))
    return [u for u in users if u.pk in ids and u.is_active]


def resolve(strategies: list[dict[str, Any]], source: Source, obj: Any, *, organization: Any, actor: Any = None, exclude_actor: bool = True) -> list[Any]:
    """The deduplicated, authorized, bounded list of recipients."""
    location = source.location(obj)
    pool: list[Any] = []
    for strategy in strategies:
        kind = strategy.get("type")
        if kind == k.Strategy.ASSIGNEE:
            pool.extend(_by_id(source.assignee_id(obj)))
        elif kind == k.Strategy.CREATOR:
            pool.extend(_by_id(source.creator_id(obj)))
        elif kind == k.Strategy.TASK_ASSIGNEES:
            if source.type == "workflow_task":
                if obj.assignee_id:
                    pool.extend(_by_id(obj.assignee_id))
                else:
                    pool.extend(policy_matcher.candidates(obj.state.assignee_policy, organization, location))
        else:
            policy = as_policy(strategy)
            if policy is not None:
                pool.extend(policy_matcher.candidates(policy, organization, location))
    seen: set[Any] = set()
    ordered: list[Any] = []
    for user in pool:
        if user.pk not in seen:
            seen.add(user.pk)
            ordered.append(user)
    ordered = _members_ok(ordered, organization)
    if exclude_actor and actor is not None:
        ordered = [u for u in ordered if u.pk != getattr(actor, "pk", None)]
    allowed = [u for u in ordered if source.can_view(u, obj)]
    allowed.sort(key=lambda u: str(u.email))  # deterministic
    return allowed[: k.MAX_RECIPIENTS]
