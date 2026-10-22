"""Private evidence storage: a small interface (``EvidenceStorage``) and the filesystem backend.

Rules (ADR 0060): evidence is **never** under ``MEDIA_ROOT`` / ``STATIC_ROOT`` or any directory a web server publishes; files live
under ``settings.EVIDENCE_ROOT`` at *generated, unpredictable* keys; a key is validated against a strict pattern and the resolved path
is checked to be inside the root, so no filename, traversal sequence or symlink trick from a user can reach it.  Writes are atomic
(temp file + rename).  Nothing here knows about URLs - files are only ever read back through an authorized view.

An object-storage adapter (S3, Azure Blob...) subclasses ``EvidenceStorage`` and implements the same four methods; select it with
``EVIDENCE_STORAGE``.  It must use private buckets, server-side encryption and no public ACLs (docs/evidence-storage.md).
"""

from __future__ import annotations

import os
import re
import secrets
import tempfile
from abc import ABC, abstractmethod
from pathlib import Path
from typing import BinaryIO

from django.conf import settings
from django.utils.module_loading import import_string

KEY_RE = re.compile(r"^[0-9a-f]{2}/[0-9a-f]{2}/[0-9a-f]{32}$")


class StorageError(Exception):
    """A storage operation failed (the caller compensates: nothing is left half-written)."""


def new_key() -> str:
    """An unpredictable key: 128 random bits, fanned out into two directory levels so no directory grows huge."""
    token = secrets.token_hex(16)
    return f"{token[:2]}/{token[2:4]}/{token}"


class EvidenceStorage(ABC):
    @abstractmethod
    def put(self, key: str, data: bytes) -> None:
        """Store ``data`` under ``key`` atomically; never overwrite an existing key."""

    @abstractmethod
    def open(self, key: str) -> BinaryIO:
        """Open the stored bytes for reading (raises ``StorageError`` if missing)."""

    @abstractmethod
    def delete(self, key: str) -> None:
        """Remove the stored bytes (idempotent).  Used only for compensation and reconciliation - never for retention."""

    @abstractmethod
    def exists(self, key: str) -> bool: ...

    def keys(self):  # noqa: ANN201
        """Every stored key (for the orphan reconciliation command); optional for backends that cannot list cheaply."""
        raise NotImplementedError


class FileSystemEvidenceStorage(EvidenceStorage):
    def __init__(self, root: str | Path | None = None) -> None:
        self.root = Path(root or settings.EVIDENCE_ROOT).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        if hasattr(os, "chmod") and os.name != "nt":
            os.chmod(self.root, 0o700)

    def _path(self, key: str) -> Path:
        if not isinstance(key, str) or not KEY_RE.match(key):
            raise StorageError("Invalid storage key.")
        path = (self.root / key).resolve()
        if self.root not in path.parents:  # a symlink or odd key can never lead outside the root
            raise StorageError("Invalid storage key.")
        return path

    def put(self, key: str, data: bytes) -> None:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            raise StorageError("The storage key already exists.")
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".part-")
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, path)
        except OSError as exc:
            Path(tmp).unlink(missing_ok=True)
            raise StorageError("The file could not be stored.") from exc

    def open(self, key: str) -> BinaryIO:
        try:
            return self._path(key).open("rb")
        except OSError as exc:
            raise StorageError("The stored file is not available.") from exc

    def delete(self, key: str) -> None:
        try:
            self._path(key).unlink(missing_ok=True)
        except OSError as exc:
            raise StorageError("The file could not be removed.") from exc

    def exists(self, key: str) -> bool:
        try:
            return self._path(key).is_file()
        except StorageError:
            return False

    def keys(self):  # noqa: ANN201
        for path in self.root.rglob("*"):
            if path.is_file() and not path.name.startswith(".part-"):
                key = path.relative_to(self.root).as_posix()
                if KEY_RE.match(key):
                    yield key

    def partials(self):  # noqa: ANN201
        yield from (p for p in self.root.rglob(".part-*") if p.is_file())


def get_storage() -> EvidenceStorage:
    return import_string(settings.EVIDENCE_STORAGE)()
