"""Controlled extension points of the version lifecycle - registered by other apps (Phase 15 scoring), never edited here.

A version may own *extra definition data* that lives in another app but must follow the version's lifecycle exactly: it is sealed
by the integrity hash, validated before publication, copied when a draft is cloned, and compared between versions.  The
checklists app does not import those apps; they register here when Django starts.

* ``manifest_extensions``: ``ext(version) -> (key, data) | None``.  ``data`` is a JSON-able canonical description; ``None`` (nothing
  configured) adds nothing, so a version without extra data keeps exactly the hash it always had.
* ``publication_validators``: ``validate(version) -> list[(level, code, message, subject)]`` with ``level`` in ``error|warning``.
* ``clone_hooks``: ``clone(source, target, actor) -> dict[str, int]`` copying the extra data into the new draft (counts are logged).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

manifest_extensions: list[Callable[[Any], tuple[str, Any] | None]] = []
publication_validators: list[Callable[[Any], list[tuple[str, str, str, str]]]] = []
clone_hooks: list[Callable[[Any, Any, Any], dict[str, int]]] = []


def register_manifest_extension(ext: Callable[[Any], tuple[str, Any] | None]) -> None:
    if ext not in manifest_extensions:
        manifest_extensions.append(ext)


def register_publication_validator(validator: Callable[[Any], list[tuple[str, str, str, str]]]) -> None:
    if validator not in publication_validators:
        publication_validators.append(validator)


def register_clone_hook(hook: Callable[[Any, Any, Any], dict[str, int]]) -> None:
    if hook not in clone_hooks:
        clone_hooks.append(hook)


def manifest_extras(version: Any) -> dict[str, Any]:
    """The extra canonical sections of ``version`` (empty when no extension has anything to add)."""
    extras: dict[str, Any] = {}
    for ext in manifest_extensions:
        got = ext(version)
        if got is not None:
            extras[got[0]] = got[1]
    return extras
