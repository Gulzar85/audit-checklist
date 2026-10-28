# 0086. Report exports and private artifacts

- **Status:** Accepted
- **Date:** 2026-10-28

## Decision
* **CSV** (UTF-8 with BOM), **XLSX** (`openpyxl`, added as a dependency - the library django-import-export already expects for XLSX) and **PDF** from a small built-in writer: WeasyPrint is listed for production but cannot load its native libraries on the Windows host, and rendering HTML to PDF would add an HTML/URL attack surface. The PDF writer uses Helvetica/WinAnsi (Latin-1 only; other characters become `?`, documented).
* **Formula injection**: text cells starting with `=`, `+`, `-`, `@`, tab or CR get a leading apostrophe (XLSX cells forced to string). Numbers and dates are untouched.
* **Artifacts** are files under `REPORT_ARTIFACT_ROOT` (outside served directories) with random 48-hex names, a size limit and a SHA-256. They are only read through `open_artifact`: requester only, permissions re-checked at download, expiry and hash verified, audited, `Cache-Control: private, no-store`. Files expire after 72 h; run metadata is deleted after 90 days.

## Consequences
No new native dependency; downloads cannot be guessed or reused after access is revoked; PDF output is limited to Latin-1 text tables.
