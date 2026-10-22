"""Reconcile private storage with the database after crashes (a file written but its rows never committed, or the reverse).

* **Orphan files** - stored bytes with no ``EvidenceAsset`` row (an upload whose transaction failed or whose process died): deleted once
  older than ``--min-age-minutes`` (so an upload in flight is never touched).  Leftover ``.part-`` temp files are removed the same way.
* **Missing files** - an asset whose bytes are gone: reported (never auto-"fixed"); restore from backup.

``--dry-run`` reports without changing anything.  Exit status is 1 when assets with missing files are found.
"""

from __future__ import annotations

import time

from django.core.management.base import BaseCommand, CommandError

from apps.evidence.models import EvidenceAsset
from apps.evidence.storage import FileSystemEvidenceStorage, get_storage


class Command(BaseCommand):
    help = "Delete orphaned evidence files and report assets whose file is missing."

    def add_arguments(self, parser) -> None:  # noqa: ANN001
        parser.add_argument("--min-age-minutes", type=int, default=60)
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args, **options) -> None:  # noqa: ANN002, ANN003
        storage = get_storage()
        known = set(EvidenceAsset.objects.values_list("storage_key", flat=True))
        cutoff = time.time() - options["min_age_minutes"] * 60
        orphans, removed = [], 0
        try:
            stored = list(storage.keys())
        except NotImplementedError:
            raise CommandError("This storage backend cannot list its files; reconcile with the provider's inventory tools.") from None
        for key in stored:
            if key in known:
                continue
            path = storage._path(key) if isinstance(storage, FileSystemEvidenceStorage) else None  # noqa: SLF001
            if path is not None and path.stat().st_mtime > cutoff:
                continue  # too recent: possibly still being finalized
            orphans.append(key)
            if not options["dry_run"]:
                storage.delete(key)
                removed += 1
        if isinstance(storage, FileSystemEvidenceStorage):
            for part in storage.partials():
                if part.stat().st_mtime <= cutoff:
                    orphans.append(part.name)
                    if not options["dry_run"]:
                        part.unlink(missing_ok=True)
                        removed += 1
        missing = [a for a in EvidenceAsset.objects.only("pk", "storage_key").iterator() if not storage.exists(a.storage_key)]
        self.stdout.write(f"Orphan files: {len(orphans)} ({'would be removed' if options['dry_run'] else f'{removed} removed'}). Assets with a missing file: {len(missing)}.")
        for asset in missing[:50]:
            self.stderr.write(f"  missing file for asset {asset.pk}")
        if missing:
            raise SystemExit(1)
