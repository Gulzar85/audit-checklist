"""Builders for the Phase 11 tests: harmless sample files, a checklist with evidence questions, scanner/storage fixtures."""

from __future__ import annotations

import io
import struct

import pytest
from PIL import Image

from apps.checklists.seed import load_template

from executions_support import build_exec_world, issued

PHOTO_CHECKLIST = {
    "code": "EVD", "name": "Evidence Checklist", "category": "Safety",
    "sections": [
        {"code": "S1", "name": "Evidence", "items": [
            {"code": "OK", "question": "Is everything in order?", "answer_type": "yes_no"},
            {"code": "PHOTO", "question": "Photo of the area", "answer_type": "photo", "required": True,
             "config": {"max_files": 2, "max_size_mb": 5, "allowed_extensions": ["jpg", "jpeg", "png", "webp"]}},
            {"code": "DOC", "question": "Supporting document", "answer_type": "file", "required": False,
             "config": {"max_files": 2, "max_size_mb": 5, "allowed_extensions": ["pdf", "jpg", "png"]}},
            {"code": "SIGN", "question": "Manager signature", "answer_type": "signature", "required": False},
            {"code": "TEMP", "question": "Temperature", "answer_type": "numeric", "required": False, "config": {"min": 0, "max": 10, "decimals": 1}},
        ]},
    ],
    "rules": [
        {"name": "A high temperature needs a photo", "condition": {"source_item_code": "TEMP", "comparison": "GREATER_THAN", "value": "8"},
         "actions": [{"type": "REQUIRE_EVIDENCE", "target": {"type": "item", "code": "TEMP"}, "kinds": ["photo"], "min_count": 1}]},
    ],
}


def jpeg_bytes(size=(64, 48), exif=True, trailer=b"") -> bytes:
    image = Image.new("RGB", size, (200, 30, 30))
    out = io.BytesIO()
    kwargs = {}
    if exif:
        exif_data = Image.Exif()
        exif_data[0x010F] = "SecretCameraMaker"  # Make
        exif_data[0x8825] = {1: "N", 2: (31.0, 0.0, 0.0)}  # GPS IFD
        kwargs["exif"] = exif_data
    image.save(out, "JPEG", **kwargs)
    return out.getvalue() + trailer


def png_bytes(size=(32, 32)) -> bytes:
    out = io.BytesIO()
    Image.new("RGBA", size, (0, 100, 200, 255)).save(out, "PNG")
    return out.getvalue()


def webp_bytes(size=(32, 32)) -> bytes:
    out = io.BytesIO()
    Image.new("RGB", size, (10, 200, 10)).save(out, "WEBP")
    return out.getvalue()


def pdf_bytes(extra: bytes = b"") -> bytes:
    return b"%PDF-1.4\n1 0 obj\n<< /Type /Catalog /Pages 2 0 R " + extra + b">>\nendobj\n2 0 obj\n<< /Type /Pages /Kids [] /Count 0 >>\nendobj\ntrailer\n<< /Root 1 0 R >>\n%%EOF\n"


def png_bomb_header(width=60000, height=60000) -> bytes:
    """A syntactically valid PNG header claiming a huge canvas (a decompression-bomb probe; tiny on disk, harmless)."""
    def chunk(kind: bytes, data: bytes) -> bytes:
        import zlib

        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    import zlib

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(b"\x00" * 16)) + chunk(b"IEND", b"")


@pytest.fixture(autouse=True)
def evidence_storage(settings, tmp_path):
    """Every evidence test writes to its own private temp directory and uses the harmless EICAR test scanner."""
    settings.EVIDENCE_ROOT = tmp_path / "private"
    settings.EVIDENCE_SCANNER = "apps.evidence.scanning.EicarTestScanner"
    settings.EVIDENCE_SCAN_REQUIRED = True
    settings.EVIDENCE_ALLOW_VIEW_PENDING = False
    return settings.EVIDENCE_ROOT


def build_evidence_world(org_a, org_b, make_user, assign, make_type):
    w = build_exec_world(org_a, org_b, make_user, assign, make_type)
    template, _ = load_template(org_a, PHOTO_CHECKLIST)
    w["vtemplate"] = template
    w["vversion"] = template.versions.get(version_number=1)
    return w


def vissued(w, location=None, **kw):
    return issued(w, location, version=w["vversion"], **kw)
