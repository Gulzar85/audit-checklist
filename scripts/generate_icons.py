"""Generate PWA icons and the favicon from code (no binary assets required).

Run from the project root::

    python scripts/generate_icons.py

Icons are written to ``static/icons``. The design is intentionally simple: a
brand-coloured rounded square with a white check mark, which scales cleanly and
is legible at small sizes.
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parent.parent
ICONS_DIR = ROOT / "static" / "icons"

BRAND = (79, 70, 229, 255)  # brand-600 (#4f46e5)
BRAND_DARK = (49, 46, 129, 255)  # brand-900
WHITE = (255, 255, 255, 255)


def _draw_check(draw: ImageDraw.ImageDraw, size: int, *, inset: float = 0.28, width: float = 0.09) -> None:
    """Draw a check mark scaled to ``size``."""
    points = [
        (0.30, 0.53),
        (0.44, 0.67),
        (0.72, 0.35),
    ]
    scaled = [(int(x * size), int(y * size)) for x, y in points]
    draw.line(scaled, fill=WHITE, width=max(2, int(size * width)), joint="curve")


def make_icon(size: int, *, rounded: bool, background: tuple[int, int, int, int]) -> Image.Image:
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    if rounded:
        radius = int(size * 0.22)
        draw.rounded_rectangle((0, 0, size - 1, size - 1), radius=radius, fill=background)
    else:
        draw.rectangle((0, 0, size - 1, size - 1), fill=background)
    _draw_check(draw, size)
    return image


def main() -> None:
    ICONS_DIR.mkdir(parents=True, exist_ok=True)

    for size in (192, 512):
        make_icon(size, rounded=True, background=BRAND).save(ICONS_DIR / f"icon-{size}.png")

    make_icon(180, rounded=True, background=BRAND).save(ICONS_DIR / "apple-touch-icon.png")
    make_icon(512, rounded=False, background=BRAND_DARK).save(ICONS_DIR / "icon-maskable-512.png")

    favicon = make_icon(64, rounded=True, background=BRAND)
    favicon.save(
        ICONS_DIR / "favicon.ico",
        sizes=[(16, 16), (32, 32), (48, 48), (64, 64)],
    )
    print(f"Icons written to {ICONS_DIR}")


if __name__ == "__main__":
    main()
