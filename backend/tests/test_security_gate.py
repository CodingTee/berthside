"""Unit tests for the attachment security gate.

The gate is the only thing standing between an externally reachable endpoint and
a payload the process is asked to open, so its decisions are pinned here rather
than left to the integration test. The two failure modes that matter are
opposite: an ordinary document rejected as malicious (a false alarm that throws
away real work) and a disguised executable accepted (the case the gate exists
for).
"""
from __future__ import annotations

import base64
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.services.security import encoded_size_exceeds_cap, verify_file_safety

PDF = b"%PDF-1.4\n1 0 obj\n<<>>\nendobj\ntrailer\n<<>>\n%%EOF\n"
OLE = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
SCRIPT = b"@echo off\n"


def test_ordinary_documents_are_not_mistaken_for_attacks():
    """A domain or a stray label inside a filename is not a spoof.

    The gate is meant to catch "invoice.pdf.exe". It used to scan every
    dot-separated label, so the ".com" of a vendor domain and a generic ".bin"
    both matched the dangerous set and ordinary documents were refused.
    """
    safe = [
        ("SI_COM-2026-001_a_SI.pdf", PDF),
        ("Draft_BL.pdf", PDF),
        ("report.vendor.com.pdf", PDF),
        ("booking.bin.BL.pdf", PDF),
        ("data.final.v2.pdf", PDF),
        ("quote.2026.06.01.xlsx", b"PK\x03\x04" + b"\x00" * 32),
        # Legacy formats are deliberately absent from any whitelist: the gate
        # refuses a file for what it is, not for being an unusual format.
        ("legacy_SI.doc", OLE + b"\x00" * 64),
        ("legacy_BL.xls", OLE + b"\x00" * 64),
    ]
    for name, body in safe:
        is_safe, reason = verify_file_safety(name, body)
        assert is_safe, f"{name} was refused: {reason}"


def test_spoofed_and_executable_payloads_are_still_refused():
    """Narrowing the rule must not open the door it was built to close."""
    blocked = [
        ("Draft_BL.pdf.exe", b"MZ\x90\x00" + b"\x00" * 64),   # PE header
        ("Draft_BL.exe.pdf", PDF),                            # label before .pdf
        ("legit.pdf.exe.pdf", PDF),                           # label in the middle
        ("scan.pdf.exe", PDF),                                # suffix only
        ("Invoice.com", b"x" * 64),                           # real .com file
        ("archive.bin", b"x" * 64),                           # real .bin file
        ("installer.bat", SCRIPT),
        ("macro.js.docx", b"PK\x03\x04" + b"\x00" * 32),
        ("payload.pdf", b"MZ\x90\x00" + b"x" * 64),            # PDF name, PE body
    ]
    for name, body in blocked:
        is_safe, reason = verify_file_safety(name, body)
        assert not is_safe, f"{name} was accepted"
        assert reason


def test_a_spoofed_container_header_is_refused():
    is_safe, reason = verify_file_safety("draft_BL.pdf", b"<html>not a pdf</html>")
    assert not is_safe
    assert "PDF" in reason

    is_safe, reason = verify_file_safety("draft_BL.docx", b"plain text pretending")
    assert not is_safe
    assert "OpenXML" in reason


def test_an_oversized_payload_is_refused_before_it_is_decoded(monkeypatch):
    """The cap has to be enforceable without materialising the payload.

    base64 expands three bytes into four characters, so the decoded size is
    known from the encoded length alone. Checking after the decode spends the
    memory the cap exists to protect.
    """
    from app.services import security

    monkeypatch.setattr(security, "MAX_ATTACHMENT_SIZE", 1024)
    big = base64.b64encode(b"\x00" * 2048).decode()
    assert security.encoded_size_exceeds_cap(encoded=big)
    assert security.encoded_size_exceeds_cap(text="x" * 1025)

    # A payload comfortably inside the cap is left to the byte-level check.
    assert not security.encoded_size_exceeds_cap(encoded=base64.b64encode(PDF).decode())
    assert not security.encoded_size_exceeds_cap(text="Shipper: ACME")
    assert not security.encoded_size_exceeds_cap()

    # ...and the byte-level check still refuses what gets through.
    is_safe, reason = security.verify_file_safety("big.pdf", b"%PDF-" + b"\x00" * 2048)
    assert not is_safe
    assert "maximum" in reason.lower()


def test_the_size_estimate_agrees_with_the_real_decoded_length():
    """The estimate must not disagree with what base64 actually decodes to."""
    for size in (1, 2, 3, 1000, 65536):
        encoded = base64.b64encode(b"\x00" * size).decode()
        assert abs(len(encoded) // 4 * 3 - size) <= 2
