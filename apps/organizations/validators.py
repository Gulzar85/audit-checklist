"""Upload validators for organization branding assets.

Client-provided filenames and MIME types are not trusted: the extension is
allow-listed, the size is capped, and the file content is verified with Pillow.
SVG is intentionally rejected because it can carry executable script content.
"""

from __future__ import annotations

from pathlib import Path

from django.core.exceptions import ValidationError
from django.utils.translation import gettext_lazy as _

try:  # Pillow is a hard dependency, but keep the import defensive.
    from PIL import Image, UnidentifiedImageError
except Exception:  # pragma: no cover - Pillow is always installed
    Image = None  # type: ignore[assignment]
    UnidentifiedImageError = Exception  # type: ignore[assignment,misc]

MAX_BRANDING_IMAGE_BYTES = 2 * 1024 * 1024  # 2 MB
ALLOWED_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
ALLOWED_IMAGE_FORMATS = {"PNG", "JPEG", "WEBP", "GIF"}


def validate_branding_image(value) -> None:  # noqa: ANN001
    """Validate an uploaded logo/favicon: extension, size and real content."""
    if value in (None, ""):
        return

    name = getattr(value, "name", "") or ""
    extension = Path(name).suffix.lower()
    if extension not in ALLOWED_IMAGE_EXTENSIONS:
        allowed = ", ".join(sorted(ALLOWED_IMAGE_EXTENSIONS))
        raise ValidationError(
            _("Unsupported image type %(ext)s. Allowed types: %(allowed)s."),
            params={"ext": extension or "(none)", "allowed": allowed},
        )

    size = getattr(value, "size", None)
    if size is not None and size > MAX_BRANDING_IMAGE_BYTES:
        raise ValidationError(
            _("Image is too large (%(size)s bytes). Maximum is %(max)s bytes."),
            params={"size": size, "max": MAX_BRANDING_IMAGE_BYTES},
        )

    if Image is None:  # pragma: no cover - defensive
        return

    # Verify the bytes really are an image (defends against a renamed payload).
    stream = getattr(value, "file", None)
    if stream is None:
        return
    try:
        position = stream.tell()
    except (AttributeError, OSError):
        position = None
    try:
        stream.seek(0)
        image = Image.open(stream)
        image.verify()
        if (image.format or "").upper() not in ALLOWED_IMAGE_FORMATS:
            raise ValidationError(_("The uploaded file is not a supported image."))
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise ValidationError(_("The uploaded file is not a valid image.")) from exc
    finally:
        if position is not None:
            stream.seek(position)
