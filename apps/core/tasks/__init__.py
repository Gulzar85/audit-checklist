"""Background task definitions for the core app.

Tasks live behind a service boundary: they should validate inputs and delegate
to ``apps.core.services``.  The configured backend (``TASKS['default']``)
decides whether work runs inline, in a worker, or is merely recorded.
"""

from __future__ import annotations

from .maintenance import ping

__all__ = ["ping"]
