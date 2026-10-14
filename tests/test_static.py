"""Static asset presence tests (self-hosted for CSP + offline)."""

from __future__ import annotations

from django.conf import settings

import pytest

STATIC = settings.BASE_DIR / "static"


@pytest.mark.parametrize(
    "relative_path",
    [
        "css/app.css",
        "js/app.js",
        "js/service-worker.js",
        "vendor/htmx.min.js",
        "vendor/alpine.min.js",
        "vendor/lucide.min.js",
    ],
)
def test_static_asset_exists(relative_path):
    assert (STATIC / relative_path).is_file(), f"missing static asset: {relative_path}"


def test_built_css_is_non_trivial():
    assert (STATIC / "css" / "app.css").stat().st_size > 5_000


def test_vendored_js_files_are_non_empty():
    for name in ("htmx.min.js", "alpine.min.js", "lucide.min.js"):
        assert (STATIC / "vendor" / name).stat().st_size > 0
