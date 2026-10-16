"""File-type contracts for the photo / file / signature answer types.

Phase 5 defines *what a valid file reference is*; storing, scanning and serving files belongs to the evidence phase.
Rules that phase must follow, and this module already enforces where it can:

* an extension is accepted only from a fixed platform allow-list (no executables, scripts, archives or HTML);
* the **content type is decided by the server** - from the extension map and, once bytes exist, by sniffing the
  magic number (:func:`verify_content`); the client's ``Content-Type`` is only a consistency check;
* files are never served from a predictable public URL: access goes through an authorized view.
"""

from __future__ import annotations

#: kind -> extensions an author may allow (the universe; a config picks a subset).
ALLOWED_EXTENSIONS: dict[str, tuple[str, ...]] = {
    "photo": ("jpg", "jpeg", "png", "webp", "heic", "heif"),
    "file": ("pdf", "doc", "docx", "xls", "xlsx", "csv", "txt", "jpg", "jpeg", "png", "webp", "heic"),
}

_CONTENT_TYPES = {
    "jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png", "webp": "image/webp",
    "heic": "image/heic", "heif": "image/heif", "pdf": "application/pdf",
    "doc": "application/msword",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "xls": "application/vnd.ms-excel",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "csv": "text/csv", "txt": "text/plain",
}


def content_type_for(extension: str) -> str:
    """The content type the platform assigns to an allowed extension."""
    return _CONTENT_TYPES[extension.lower().lstrip(".")]


def sniff_content_type(header: bytes) -> str | None:
    """Guess a content type from the first bytes of a file (magic numbers); ``None`` if unrecognised."""
    if header.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if header.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if header[:4] == b"RIFF" and header[8:12] == b"WEBP":
        return "image/webp"
    if header[4:8] == b"ftyp" and header[8:12] in (b"heic", b"heix", b"hevc", b"mif1", b"msf1"):
        return "image/heic" if header[8:12] != b"mif1" else "image/heif"
    if header.startswith(b"%PDF-"):
        return "application/pdf"
    if header.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"):
        return "application/x-ole"  # legacy .doc / .xls
    if header.startswith(b"PK\x03\x04"):
        return "application/zip"  # .docx / .xlsx container
    return None


_COMPATIBLE = {
    "application/msword": {"application/x-ole"},
    "application/vnd.ms-excel": {"application/x-ole"},
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": {"application/zip"},
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": {"application/zip"},
    "image/heif": {"image/heic", "image/heif"},
}


def verify_content(extension: str, header: bytes) -> bool:
    """Whether the bytes of a file match what its extension claims.

    Text formats (``csv``/``txt``) must contain no NUL bytes; everything else must carry the right magic number.
    Call this from the evidence phase with the first 16-32 bytes of the upload.
    """
    expected = content_type_for(extension)
    if expected in ("text/csv", "text/plain"):
        return b"\x00" not in header
    found = sniff_content_type(header)
    return found is not None and (found == expected or found in _COMPATIBLE.get(expected, set()))
