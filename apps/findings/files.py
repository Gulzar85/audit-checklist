"""Serving a finding's evidence bytes - one implementation for the web view and the API.

The file is reachable only *through its finding*: the caller has already established that the user may see the finding, the
attachment must be one of that finding's own evidence (never another record's), and the asset must be servable (available;
never quarantined or unscanned).  Images may be shown inline; everything else is a download.  Every serve is audited.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from django.http import HttpResponse

from . import selectors


def evidence_response(user: Any, finding: Any, attachment_pk: Any, *, inline: bool) -> HttpResponse | None:
    """The file response, or ``None`` when it must be answered 404 (not this finding's, quarantined, unchecked, missing, or not an image)."""
    from apps.evidence import services as evidence_services
    from apps.evidence.selectors import is_servable
    from apps.evidence.validation import IMAGE_TYPES

    found = selectors.get_evidence_attachment(finding, attachment_pk)
    if found is None or not is_servable(found.asset):
        return None
    asset = found.asset
    if inline and asset.content_type not in IMAGE_TYPES:
        return None
    try:
        data = evidence_services.read_bytes(asset)
    except evidence_services.EvidenceError:
        return None
    evidence_services.record_access(user, asset, "preview" if inline else "download")
    response = HttpResponse(data, content_type=asset.content_type)
    response["Content-Disposition"] = f"{'inline' if inline else 'attachment'}; filename*=UTF-8''{quote(asset.original_name)}"
    response["X-Content-Type-Options"] = "nosniff"
    response["Cache-Control"] = "private, no-store"
    response["Content-Security-Policy"] = "sandbox; default-src 'none'; style-src 'none'; script-src 'none'"
    response["Cross-Origin-Resource-Policy"] = "same-origin"
    response["Content-Length"] = str(len(data))
    return response
