"""Maintenance / diagnostic tasks.

A single no-op task proves the Tasks framework wiring end to end without
introducing business behaviour in Phase 0.
"""

from __future__ import annotations

from django.tasks import task


@task
def ping() -> str:
    """Return a constant so the task pipeline can be verified."""
    return "pong"
