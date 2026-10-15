"""Upload validators for organization branding assets.

Client-provided filenames and MIME types are never trusted.  An upload is
accepted only if *all* of the following hold:

* the extension is on an allow-list (executables, scripts, HTML and SVG are
  rejected - SVG because it can carry script);
* the size is within the cap;
* the file content really decodes as one of the allowed raster formats (checked
  with Pillow, which defeats a renamed payload);
* the image dimensions are sane (also guards against decompression bombs).

Stored filenames are generated server-side (see ``organization_logo_path``).
"""

from __future__ import annotations

from pathlib import Path

from django.core.exceptions import ValidationError
from django.utils.translation import gettext_lazy as _
from PIL import Image, UnidentifiedImageError

MAX_BRANDING_IMAGE_BYTES = 2 * 1024 * 1024  # 2 MB
MAX_BRANDING_IMAGE_DIMENSION = 4096  # pixels, per side
ALLOWED_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
ALLOWED_IMAGE_FORMATS = {"PNG", "JPEG", "WEBP", "GIF"}


def validate_branding_image(value) -> None:  # noqa: ANN001
    """Validate an uploaded logo/favicon: extension, size and real content."""
    if value in (None, "", False):
        return

    name = getattr(value, "name", "") or ""
    extension = Path(name).suffix.lower()
    if extension not in ALLOWED_IMAGE_EXTENSIONS:
        allowed = ", ".join(sorted(ALLOWED_IMAGE_EXTENSIONS))
        raise ValidationError(
            _("Unsupported image type %(ext)s. Allowed types: %(allowed)s."),
            params={"ext": extension or "(none)", "allowed": allowed},
            code="invalid_extension",
        )

    size = getattr(value, "size", None)
    if size is not None and size > MAX_BRANDING_IMAGE_BYTES:
        raise ValidationError(
            _("Image is too large (%(size)s bytes). Maximum is %(max)s bytes."),
            params={"size": size, "max": MAX_BRANDING_IMAGE_BYTES},
            code="file_too_large",
        )

    # Files already stored (an unchanged field on re-validation) have no open
    # in-memory stream worth re-reading; only freshly uploaded content matters.
    if getattr(value, "_committed", False):
        return

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
        image_format = (image.format or "").upper()
        width, height = image.size
        image.verify()
    except (UnidentifiedImageError, OSError, ValueError, SyntaxError, Image.DecompressionBombError) as exc:
        raise ValidationError(_("The uploaded file is not a valid image."), code="invalid_image") from exc
    finally:
        if position is not None:
            try:
                stream.seek(position)
            except (OSError, ValueError):  # pragma: no cover - closed stream
                pass

    if image_format not in ALLOWED_IMAGE_FORMATS:
        raise ValidationError(_("The uploaded file is not a supported image."), code="invalid_image")
    if max(width, height) > MAX_BRANDING_IMAGE_DIMENSION:
        raise ValidationError(
            _("Image is too large (%(w)s×%(h)s). Maximum is %(max)s pixels per side."),
            params={"w": width, "h": height, "max": MAX_BRANDING_IMAGE_DIMENSION},
            code="dimensions_too_large",
        )
