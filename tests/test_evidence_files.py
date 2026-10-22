"""Evidence file security: validation pipeline, private storage, scanner adapters.  Pure unit tests (no database) - harmless fixtures only."""

from __future__ import annotations

import io
import socket
import struct
import threading

import pytest
from PIL import Image
from django.test import override_settings

from apps.evidence import scanning, validation
from apps.evidence.storage import FileSystemEvidenceStorage, StorageError, new_key
from apps.evidence.validation import Rejected, inspect_upload, sanitize_name

from evidence_support import evidence_storage, jpeg_bytes, pdf_bytes, png_bomb_header, png_bytes, webp_bytes  # noqa: F401


def code_of(call):
    with pytest.raises(Rejected) as exc:
        call()
    return exc.value.code


class TestAllowedFormats:
    def test_images_and_pdf_are_accepted_with_the_servers_own_type_and_hash(self):
        for name, data, ctype, kind in (("a.jpg", jpeg_bytes(), "image/jpeg", "photo"), ("a.png", png_bytes(), "image/png", "photo"),
                                         ("a.webp", webp_bytes(), "image/webp", "photo"), ("a.pdf", pdf_bytes(), "application/pdf", "file")):
            r = inspect_upload(name, data, kind_hint="file" if ctype == "application/pdf" else "photo")
            assert (r.content_type, r.kind) == (ctype, kind) and len(r.sha256) == 64 and r.data

    def test_the_hash_is_of_the_stored_bytes_not_the_upload(self):
        import hashlib

        original = jpeg_bytes()
        r = inspect_upload("a.jpg", original, kind_hint="photo")
        assert r.sha256 == hashlib.sha256(r.data).hexdigest() and r.data != original

    def test_signature_images_are_png_or_jpeg_kind_signature(self):
        assert inspect_upload("sig.png", png_bytes(), kind_hint="signature").kind == "signature"
        assert code_of(lambda: inspect_upload("sig.pdf", pdf_bytes(), kind_hint="signature")) == "type_not_allowed"

    def test_a_photo_question_refuses_a_pdf(self):
        assert code_of(lambda: inspect_upload("a.pdf", pdf_bytes(), kind_hint="photo")) == "type_not_allowed"

    def test_the_question_configuration_narrows_the_types(self):
        assert code_of(lambda: inspect_upload("a.png", png_bytes(), kind_hint="photo", allowed_extensions=("jpg",))) == "type_not_allowed"
        assert inspect_upload("a.jpeg", jpeg_bytes(), kind_hint="photo", allowed_extensions=("jpg",)).content_type == "image/jpeg"


class TestRejections:
    @pytest.mark.parametrize("name", ["run.exe", "page.html", "image.svg", "x.js", "a.php", "archive.zip", "doc.docx", "noext", "a.jpg.exe", "photo.heic", "x.gif", "a.pdf.html"])
    def test_disallowed_extensions(self, name):
        assert code_of(lambda: inspect_upload(name, jpeg_bytes(), kind_hint="file")) == "type_not_allowed"

    def test_mime_spoofing_by_content(self):
        assert code_of(lambda: inspect_upload("photo.jpg", b"<html><script>alert(1)</script></html>", kind_hint="photo")) == "content_mismatch"
        assert code_of(lambda: inspect_upload("photo.png", jpeg_bytes(), kind_hint="photo")) == "content_mismatch"  # a JPEG renamed .png
        assert code_of(lambda: inspect_upload("doc.pdf", jpeg_bytes(), kind_hint="file")) == "content_mismatch"
        assert code_of(lambda: inspect_upload("photo.jpg", b"MZ\x90\x00" + b"\x00" * 100, kind_hint="photo")) == "content_mismatch"  # a PE executable
        assert code_of(lambda: inspect_upload("photo.jpg", b"<svg xmlns='http://www.w3.org/2000/svg'><script>1</script></svg>", kind_hint="photo")) == "content_mismatch"

    def test_malformed_and_truncated_images(self):
        good = jpeg_bytes()
        assert code_of(lambda: inspect_upload("a.jpg", good[: len(good) // 2], kind_hint="photo")) == "invalid_image"
        assert code_of(lambda: inspect_upload("a.jpg", b"\xff\xd8\xff" + b"\x00" * 50, kind_hint="photo")) == "invalid_image"
        assert code_of(lambda: inspect_upload("a.png", png_bytes()[:40], kind_hint="photo")) == "invalid_image"

    def test_oversized_files_and_empty_files(self):
        with override_settings(EVIDENCE_MAX_FILE_BYTES=1000):
            assert code_of(lambda: inspect_upload("a.jpg", jpeg_bytes(size=(300, 300)), kind_hint="photo")) == "file_too_large"
        assert code_of(lambda: inspect_upload("a.jpg", b"", kind_hint="photo")) == "empty_file"

    def test_decompression_bombs_are_refused(self):
        assert code_of(lambda: inspect_upload("a.png", png_bomb_header(), kind_hint="photo")) in ("image_too_large", "invalid_image")
        with override_settings(EVIDENCE_MAX_PIXELS=1000):
            assert code_of(lambda: inspect_upload("a.jpg", jpeg_bytes(size=(100, 100)), kind_hint="photo")) in ("image_too_large", "invalid_image")

    def test_pdfs_with_active_content_or_damage_are_refused(self):
        for extra in (b"/OpenAction << /S /JavaScript /JS (app.alert(1)) >> ", b"/Names << /JavaScript 3 0 R >> ", b"/Launch ", b"/EmbeddedFile "):
            assert code_of(lambda: inspect_upload("a.pdf", pdf_bytes(extra), kind_hint="file")) == "pdf_active_content", extra
        assert code_of(lambda: inspect_upload("a.pdf", b"%PDF-1.4\n" + b"x" * 100, kind_hint="file")) == "invalid_pdf"  # no EOF marker

    def test_appended_payloads_do_not_survive_re_encoding(self):
        r = inspect_upload("a.jpg", jpeg_bytes(trailer=b"<?php system($_GET['c']); ?>MALICIOUS"), kind_hint="photo")
        assert b"MALICIOUS" not in r.data and b"<?php" not in r.data


class TestMetadataAndNames:
    def test_exif_and_gps_are_stripped_from_images(self):
        original = jpeg_bytes(exif=True)
        assert b"SecretCameraMaker" in original
        r = inspect_upload("a.jpg", original, kind_hint="photo")
        assert b"SecretCameraMaker" not in r.data and b"Exif" not in r.data
        assert not Image.open(io.BytesIO(r.data)).getexif()
        assert (r.width, r.height) == (64, 48)

    @pytest.mark.parametrize("raw", ["../../etc/passwd", "..\\..\\windows\\system32\\evil.jpg", "C:\\temp\\a.jpg", "/abs/path/x.jpg", "a/b/c.jpg", "x\x00.jpg", "  ..  ", ""])
    def test_malicious_names_become_safe_display_names(self, raw):
        name = sanitize_name(raw)
        assert "/" not in name and "\\" not in name and ".." not in name and "\x00" not in name and name

    def test_the_stored_name_never_carries_a_path(self):
        r = inspect_upload("../../etc/cron.d/evil.jpg", jpeg_bytes(), kind_hint="photo")
        assert r.name == "evil.jpg"

    def test_long_unicode_names_are_bounded(self):
        assert len(sanitize_name("é" * 500 + ".jpg")) <= 120


class TestPrivateStorage:
    def test_keys_are_random_and_files_round_trip(self, tmp_path):
        storage = FileSystemEvidenceStorage(tmp_path)
        keys = {new_key() for _ in range(50)}
        assert len(keys) == 50 and all(len(k) == 38 for k in keys)
        key = new_key()
        storage.put(key, b"hello")
        assert storage.exists(key) and storage.open(key).read() == b"hello"
        storage.delete(key)
        storage.delete(key)  # idempotent
        assert not storage.exists(key)

    @pytest.mark.parametrize("bad", ["../x", "../../etc/passwd", "/etc/passwd", "ab/cd/../ef", "ab/cd/ABCDEF", "", "a", "ab/cd/" + "g" * 32, "..\\x", "ab/cd/" + "0" * 31])
    def test_traversal_and_malformed_keys_are_refused(self, tmp_path, bad):
        storage = FileSystemEvidenceStorage(tmp_path)
        with pytest.raises(StorageError):
            storage.put(bad, b"x")
        assert storage.exists(bad) is False
        assert not (tmp_path.parent / "x").exists()

    def test_a_key_is_never_overwritten(self, tmp_path):
        storage = FileSystemEvidenceStorage(tmp_path)
        key = new_key()
        storage.put(key, b"one")
        with pytest.raises(StorageError):
            storage.put(key, b"two")
        assert storage.open(key).read() == b"one"

    def test_the_root_is_not_a_web_served_directory(self, settings):
        from pathlib import Path

        root = Path(settings.EVIDENCE_ROOT).resolve()
        for served in (settings.STATIC_ROOT, settings.MEDIA_ROOT, *settings.STATICFILES_DIRS):
            served = Path(served).resolve()
            assert root != served and served not in root.parents and root not in served.parents, f"evidence root overlaps {served}"

    def test_no_temp_files_remain_after_a_failed_write(self, tmp_path, monkeypatch):
        storage = FileSystemEvidenceStorage(tmp_path)
        key = new_key()
        monkeypatch.setattr("os.replace", lambda *a: (_ for _ in ()).throw(OSError("disk full")))
        with pytest.raises(StorageError):
            storage.put(key, b"data")
        assert not list(tmp_path.rglob(".part-*")) and not storage.exists(key)


class FakeClamd:
    """A tiny in-process ClamAV daemon double speaking the INSTREAM protocol."""

    def __init__(self, reply: bytes):
        self.reply, self.received = reply, b""
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(1)
        self.port = self.sock.getsockname()[1]
        self.thread = threading.Thread(target=self.serve, daemon=True)
        self.thread.start()

    def serve(self):
        conn, _ = self.sock.accept()
        with conn:
            buf = b""
            while b"\0" not in buf:
                buf += conn.recv(64)
            while True:
                header = b""
                while len(header) < 4:
                    header += conn.recv(4 - len(header))
                (n,) = struct.unpack("!I", header)
                if n == 0:
                    break
                chunk = b""
                while len(chunk) < n:
                    chunk += conn.recv(n - len(chunk))
                self.received += chunk
            conn.sendall(self.reply)
        self.sock.close()


class TestScanners:
    def test_the_unconfigured_scanner_never_says_clean(self):
        assert scanning.UnconfiguredScanner().scan(b"anything").status == "error"

    def test_the_eicar_double_flags_only_the_test_string(self):
        assert scanning.EicarTestScanner().scan(b"harmless").status == "clean"
        assert scanning.EicarTestScanner().scan(b"prefix" + scanning.EICAR + b"suffix").status == "infected"

    @pytest.mark.parametrize("reply,status", [(b"stream: OK\0", "clean"), (b"stream: Eicar-Test-Signature FOUND\0", "infected"), (b"stream: size limit exceeded ERROR\0", "error"), (b"garbage\0", "error")])
    def test_clamd_protocol_outcomes(self, reply, status):
        fake = FakeClamd(reply)
        with override_settings(EVIDENCE_CLAMD_HOST="127.0.0.1", EVIDENCE_CLAMD_PORT=fake.port, EVIDENCE_CLAMD_TIMEOUT=3):
            result = scanning.ClamdScanner().scan(b"x" * 200_000)
        fake.thread.join(timeout=3)
        assert result.status == status and len(fake.received) == 200_000

    def test_an_unreachable_scanner_is_an_error_never_clean(self):
        with override_settings(EVIDENCE_CLAMD_HOST="127.0.0.1", EVIDENCE_CLAMD_PORT=1, EVIDENCE_CLAMD_TIMEOUT=1):
            assert scanning.ClamdScanner().scan(b"x").status == "error"


def test_limits_are_described_for_clients():
    limits = validation.describe_limits()
    assert limits["extensions"] == ["jpeg", "jpg", "pdf", "png", "webp"] and limits["max_file_bytes"] > 0
