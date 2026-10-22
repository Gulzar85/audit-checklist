# Evidence operations: configuration, failure recovery, testing, limitations

## Environment variables

| Variable | Default | Purpose |
|---|---|---|
| `EVIDENCE_ROOT` | `<project>/private_evidence` | private storage directory (never web-served) |
| `EVIDENCE_STORAGE` | filesystem backend | dotted path of an `EvidenceStorage` backend |
| `EVIDENCE_MAX_FILE_BYTES` / `EVIDENCE_MAX_REQUEST_BYTES` | 10 MB / 12 MB | upload size limits |
| `EVIDENCE_MAX_PIXELS` | 40 000 000 | image size guard |
| `EVIDENCE_MAX_PER_TARGET` | 10 | attachments per question |
| `EVIDENCE_SCANNER` | `UnconfiguredScanner` | scanner adapter |
| `EVIDENCE_SCAN_REQUIRED` | `true` (dev: `false`) | files are unavailable until a scanner says clean |
| `EVIDENCE_CLAMD_HOST` / `_PORT` / `_TIMEOUT` | 127.0.0.1 / 3310 / 10 | ClamAV daemon |
| `EVIDENCE_ALLOW_VIEW_PENDING` | `false` | let users see files awaiting the check |
| `API_THROTTLE_EVIDENCE_UPLOAD` | `60/min` | upload throttle scope |

## Deployment steps

1. `python manage.py migrate` (creates `evidence_*`, installs the integrity triggers via `post_migrate`).
2. Create the private evidence directory outside any served path, owned by the application user, on an encrypted volume.
3. Run/configure the scanner and set `EVIDENCE_SCANNER` / `EVIDENCE_SCAN_REQUIRED=true`.
4. Schedule `scan_evidence` (every few minutes) and `reconcile_evidence` (daily, `--dry-run` in monitoring).
5. Include the evidence directory in backups next to the database; test a restore with `reconcile_evidence --dry-run`.
6. Proxy limits: allow request bodies at least `EVIDENCE_MAX_REQUEST_BYTES` on `/api/v1/evidence/` and `/evidence/`.

## Failure recovery

| Situation | What happens |
|---|---|
| storage write fails | no rows are created; `503 storage_failed`; retry is safe |
| database fails after the file was stored | the file is deleted (compensation) |
| process dies between store and commit | an orphan file with no row; `reconcile_evidence` deletes it after `--min-age-minutes` (default 60); never public |
| scanner down | file stays `pending_scan` (not served, not counted); `scan_evidence` retries |
| infected file | `quarantined` (final); audit `evidence.quarantined` |
| duplicate / retried upload | idempotent by `operation_id` |
| file missing on disk | `reconcile_evidence` reports the asset (exit 1); restore from backup |

## Testing

```bash
python -m pytest tests/test_evidence_files.py tests/test_evidence_services.py tests/test_evidence_web_api.py tests/test_evidence_offline.py
node --test static/js/offline/tests/*.test.js
```

Tests use a temporary private directory per test, an EICAR scanner double and in-process doubles (a fake ClamAV daemon); they use harmless
fixtures only and never need real object storage or a real scanner.

## Known limitations

No HEIC/Office/SVG/video; no chunked or resumable uploads; PDFs are checked, not sanitised; no correction of evidence after submission;
files are not encrypted by the application; one storage backend ships (filesystem); malware scanning and production object storage need
external configuration; the signature question takes an uploaded signature image (no drawing pad yet).
