"""Private artifact storage: random, unguessable file names under ``REPORT_ARTIFACT_ROOT`` (outside static / media; never served by the web server).  A file is only ever opened
through a ``ReportRun`` row whose requester is re-authorized; the name comes from the database, never from a request."""

from __future__ import annotations

import hashlib
import os
import re
import secrets
from pathlib import Path

from django.conf import settings

NAME = re.compile(r"^[0-9a-f]{48}\.(csv|xlsx|pdf)$")
MAX_BYTES = 50 * 1024 * 1024


def root() -> Path:
    path = Path(getattr(settings, "REPORT_ARTIFACT_ROOT", None) or Path(settings.BASE_DIR) / "private" / "reports")
    path.mkdir(parents=True, exist_ok=True)
    return path


def store(data: bytes, ext: str) -> tuple[str, int, str]:
    """Write ``data`` atomically under a random name.  Returns ``(name, size, sha256)``."""
    if len(data) > MAX_BYTES:
        from ..errors import ReportError

        raise ReportError("The export is too large. Narrow the filters.", "too_large")
    name = f"{secrets.token_hex(24)}.{ext}"
    target = root() / name
    temp = root() / f".{name}.part"
    with open(temp, "wb") as handle:
        handle.write(data)
    os.replace(temp, target)
    return name, len(data), hashlib.sha256(data).hexdigest()


def read(name: str, sha256: str) -> bytes | None:
    """The artifact bytes, or ``None`` when missing, malformed or altered."""
    if not NAME.match(name or ""):
        return None
    path = root() / name
    try:
        data = path.read_bytes()
    except OSError:
        return None
    return data if hashlib.sha256(data).hexdigest() == sha256 else None


def remove(name: str) -> bool:
    if not NAME.match(name or ""):
        return False
    try:
        (root() / name).unlink()
        return True
    except OSError:
        return False
