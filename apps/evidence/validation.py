"""Server-side inspection of an upload (ADR 0060).  Pure functions: bytes in, a verdict (and cleaned bytes) out.

Nothing the client says is trusted: the extension is checked against a platform allow-list, the real type is decided from the
*bytes*, a raster image is fully decoded and **re-encoded** (dropping EXIF/GPS and any appended payload) and a PDF is structurally
checked and refused if it carries active content.  The cryptographic hash is computed here over the bytes that will be stored.
"""

from __future__ import annotations

import hashlib
import io
import re
from dataclasses import dataclass
from pathlib import PurePosixPath, PureWindowsPath
from typing import Any

from django.conf import settings
from PIL import Image, ImageOps, UnidentifiedImageError

from apps.checklists.answer_types.files import sniff_content_type

from .constants import Kind

#: Approved formats (a subset of the Phase 5 universe).  Office formats and HEIC are deliberately excluded: they cannot be made safe here.
APPROVED = {
    "jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png", "webp": "image/webp", "pdf": "application/pdf",
}
IMAGE_TYPES = ("image/jpeg", "image/png", "image/webp")
EXTENSION_FOR = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp", "application/pdf": "pdf"}
SIGNATURE_MAX_BYTES = 1_000_000

_PDF_ACTIVE = re.compile(rb"/(JavaScript|JS|Launch|EmbeddedFile|RichMedia|XFA|OpenAction|AA)\b")
_UNSAFE_NAME = re.compile(r"[^A-Za-z0-9._ ()\-]")


class Rejected(Exception):
    """The upload is refused; ``code`` is stable for clients, ``message`` is safe to show (no paths, no internals)."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code, self.message = code, message


@dataclass
class Inspected:
    data: bytes  # the bytes to store (re-encoded for images)
    content_type: str
    kind: str
    sha256: str
    width: int | None = None
    height: int | None = None
    name: str = ""


def sanitize_name(raw: str | None) -> str:
    """A *display* name: base name only, no separators/control characters, bounded.  Never used to build a path."""
    name = PureWindowsPath(PurePosixPath(str(raw or "")).name).name.strip().strip(".")
    name = _UNSAFE_NAME.sub("_", name)[:120].strip()
    return name or "file"


def extension_of(name: str) -> str:
    return name.rsplit(".", 1)[-1].lower() if "." in name else ""


def inspect_upload(raw_name: str, data: bytes, *, kind_hint: str, allowed_extensions: tuple[str, ...] | None = None) -> Inspected:
    """Validate and (for images) re-encode.  ``kind_hint`` is the *target's* expectation (photo / file / signature)."""
    limit = int(settings.EVIDENCE_MAX_FILE_BYTES)
    if not data:
        raise Rejected("empty_file", "The file is empty.")
    if len(data) > limit:
        raise Rejected("file_too_large", f"The file is larger than the {limit // (1024 * 1024)} MB limit.")
    name = sanitize_name(raw_name)
    ext = extension_of(name)
    if ext not in APPROVED:
        raise Rejected("type_not_allowed", "This file type is not allowed. Allowed: JPG, PNG, WEBP, PDF.")
    if allowed_extensions and ext not in allowed_extensions and not (ext == "jpg" and "jpeg" in allowed_extensions) and not (ext == "jpeg" and "jpg" in allowed_extensions):
        raise Rejected("type_not_allowed", "This question does not accept that file type.")
    sniffed = sniff_content_type(data[:32])
    if sniffed != APPROVED[ext]:
        raise Rejected("content_mismatch", "The file's content does not match its type.")
    if sniffed in IMAGE_TYPES:
        image = _reencode(data, sniffed)
        content_type, out, width, height = sniffed, image[0], image[1], image[2]
        kind = Kind.SIGNATURE if kind_hint == Kind.SIGNATURE else Kind.PHOTO
        if kind == Kind.SIGNATURE and len(out) > SIGNATURE_MAX_BYTES:
            raise Rejected("file_too_large", "The signature image is too large.")
    else:
        if kind_hint in (Kind.PHOTO, Kind.SIGNATURE):
            raise Rejected("type_not_allowed", "This question needs an image.")
        _check_pdf(data)
        content_type, out, width, height, kind = "application/pdf", data, None, None, Kind.FILE
    stored_name = f"{name.rsplit('.', 1)[0]}.{ext}"
    return Inspected(data=out, content_type=content_type, kind=str(kind), sha256=hashlib.sha256(out).hexdigest(), width=width, height=height, name=stored_name[:255])


def _reencode(data: bytes, content_type: str) -> tuple[bytes, int, int]:
    max_pixels = int(settings.EVIDENCE_MAX_PIXELS)
    Image.MAX_IMAGE_PIXELS = max_pixels  # Pillow refuses decompression bombs beyond this
    try:
        with Image.open(io.BytesIO(data)) as probe:
            probe.verify()
        with Image.open(io.BytesIO(data)) as image:
            if image.width * image.height > max_pixels:
                raise Rejected("image_too_large", "The image has too many pixels.")
            image.load()  # fully decodes: a truncated or corrupt image fails here
            image = ImageOps.exif_transpose(image)
            out = io.BytesIO()
            fmt = {"image/jpeg": "JPEG", "image/png": "PNG", "image/webp": "WEBP"}[content_type]
            if fmt == "JPEG":
                image = image.convert("RGB")
                image.save(out, "JPEG", quality=88, optimize=True)
            elif fmt == "PNG":
                image.save(out, "PNG", optimize=True)
            else:
                image.save(out, "WEBP", quality=88)
            return out.getvalue(), image.width, image.height
    except Rejected:
        raise
    except (UnidentifiedImageError, Image.DecompressionBombError, OSError, ValueError, SyntaxError, Image.DecompressionBombWarning):
        raise Rejected("invalid_image", "The image is damaged or cannot be read.") from None


def _check_pdf(data: bytes) -> None:
    if not data.startswith(b"%PDF-") or b"%%EOF" not in data[-2048:]:
        raise Rejected("invalid_pdf", "The PDF is damaged or incomplete.")
    if _PDF_ACTIVE.search(data):
        raise Rejected("pdf_active_content", "PDFs with scripts, launch actions or embedded files are not accepted.")


def describe_limits() -> dict[str, Any]:
    return {
        "extensions": sorted(APPROVED), "content_types": sorted(set(APPROVED.values())), "max_file_bytes": int(settings.EVIDENCE_MAX_FILE_BYTES),
        "max_request_bytes": int(settings.EVIDENCE_MAX_REQUEST_BYTES), "max_pixels": int(settings.EVIDENCE_MAX_PIXELS),
        "max_per_target": int(settings.EVIDENCE_MAX_PER_TARGET),
    }
