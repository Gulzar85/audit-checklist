"""Malware scanning: an interface, three adapters and the safe policy around them (ADR 0061).

``Scanner.scan(data) -> ScanResult``.  The platform never marks a file clean because scanning was skipped or failed:

* ``UnconfiguredScanner`` (the default) always answers ``error`` - with ``EVIDENCE_SCAN_REQUIRED=True`` (the default) the file then stays
  *pending* and unavailable; with the explicit development setting ``False`` the file is recorded as ``not_required`` (never "clean").
* ``ClamdScanner`` talks to a ClamAV daemon over TCP (``INSTREAM``); a timeout or any protocol problem is ``error`` (retryable).
* ``EicarTestScanner`` is a development/test double that flags the harmless EICAR test string, so the INFECTED path can be exercised
  without real malware.  It is not a malware scanner.
"""

from __future__ import annotations

import socket
import struct
from dataclasses import dataclass

from django.conf import settings
from django.utils.module_loading import import_string

EICAR = b"X5O!P%@AP[4\\PZX54(P^)7CC)7}$EICAR-STANDARD-ANTIVIRUS-TEST-FILE!$H+H*"


@dataclass(frozen=True)
class ScanResult:
    status: str  # "clean" | "infected" | "error"
    detail: str = ""


class Scanner:
    def scan(self, data: bytes) -> ScanResult:  # pragma: no cover - interface
        raise NotImplementedError


class UnconfiguredScanner(Scanner):
    def scan(self, data: bytes) -> ScanResult:
        return ScanResult("error", "No malware scanner is configured.")


class EicarTestScanner(Scanner):
    """Development / tests only: recognises the EICAR test string and calls everything else clean."""

    def scan(self, data: bytes) -> ScanResult:
        return ScanResult("infected", "EICAR-Test-File") if EICAR in data else ScanResult("clean", "")


class ClamdScanner(Scanner):
    """ClamAV ``INSTREAM`` over TCP (``EVIDENCE_CLAMD_HOST`` / ``_PORT`` / ``_TIMEOUT``)."""

    CHUNK = 64 * 1024

    def scan(self, data: bytes) -> ScanResult:
        try:
            with socket.create_connection((settings.EVIDENCE_CLAMD_HOST, int(settings.EVIDENCE_CLAMD_PORT)), timeout=int(settings.EVIDENCE_CLAMD_TIMEOUT)) as sock:
                sock.settimeout(int(settings.EVIDENCE_CLAMD_TIMEOUT))
                sock.sendall(b"zINSTREAM\0")
                for start in range(0, len(data), self.CHUNK):
                    chunk = data[start:start + self.CHUNK]
                    sock.sendall(struct.pack("!I", len(chunk)) + chunk)
                sock.sendall(struct.pack("!I", 0))
                reply = b""
                while not reply.endswith(b"\0") and len(reply) < 4096:
                    part = sock.recv(1024)
                    if not part:
                        break
                    reply += part
        except OSError:
            return ScanResult("error", "The scanner could not be reached.")
        text = reply.rstrip(b"\0\n").decode("utf-8", "replace")
        if text.endswith("OK"):
            return ScanResult("clean")
        if text.endswith("FOUND"):
            return ScanResult("infected", text.removeprefix("stream:").removesuffix("FOUND").strip()[:100])
        return ScanResult("error", "The scanner gave an unexpected answer.")


def get_scanner() -> Scanner:
    return import_string(settings.EVIDENCE_SCANNER)()


def scan_required() -> bool:
    return bool(settings.EVIDENCE_SCAN_REQUIRED)
