"""Background task framework wiring."""

from __future__ import annotations

from apps.core.tasks.maintenance import ping


def test_ping_task_runs_on_immediate_backend():
    result = ping.enqueue()
    assert result.status == "SUCCESSFUL"
    assert result.return_value == "pong"


def test_ping_task_metadata():
    assert ping.name == "ping"
