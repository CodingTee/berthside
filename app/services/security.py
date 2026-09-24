"""Security and sanitization gate for incoming attachments.

Protects the BerthSide verification pipeline against:
1. Executable and script payloads disguised as documents (.exe, .bat, .vbs, .scr, etc.).
2. Double-extension spoofing (e.g., 'Draft_BL.pdf.exe').
3. File-header / magic-bytes spoofing (PE headers 'MZ', ELF headers, Mach-O).
4. Decompression bomb / resource exhaustion attacks.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

# Dangerous extensions that should never be processed as shipping documents
DANGEROUS_EXTENSIONS = {
    ".exe", ".dll", ".bat", ".cmd", ".vbs", ".vbe", ".js", ".jse",
    ".wsf", ".wsh", ".ps1", ".psm1", ".scr", ".pif", ".hta", ".cpl",
    ".jar", ".com", ".sh", ".bash", ".bin",
}

# Extensions that can execute when a file is opened. Used by the
# double-extension rule, which is about a dangerous label sitting *before* the
# final extension ("Draft_BL.exe.pdf"). `.com` and `.bin` are left out of that
# rule because they are ordinary words inside a filename: "report.vendor.com.pdf"
# carries the ".com" of a domain, and "booking.bin.BL.pdf" a stray label. Both
# are still blocked when they are the real trailing extension, and a genuine
# PE/ELF body is caught by the magic-byte checks below regardless.
EXECUTABLE_EXTENSIONS = DANGEROUS_EXTENSIONS - {".com", ".bin"}

# There is deliberately no whitelist of "legitimate" extensions. Rejecting
# anything absent from such a list turns an unsupported format into a security
# incident, while the reader ladder already escalates it honestly as
# `unreadable`. A file is suspicious because of its extension and its bytes, not
# because it is unusual.

# Maximum allowed file size for attachments (30 MB)
MAX_ATTACHMENT_SIZE = 30 * 1024 * 1024

# Leading bytes each container/document format must carry. Grouped by the
# suffixes that claim them, because several suffixes share one format (an .odt
# and an .ods are both ZIPs; .pages is a ZIP too).
_CONTAINER_HEADERS: tuple[tuple[tuple[str, ...], tuple[bytes, ...], str], ...] = (
    ((".zip",), (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08"), "ZIP archive"),
    ((".odt", ".ods", ".odp", ".ott", ".ots", ".otp"),
     (b"PK\x03\x04",), "OpenDocument document"),
    ((".pages", ".numbers", ".key"), (b"PK\x03\x04",), "iWork package"),
    ((".7z",), (b"7z\xbc\xaf\x27\x1c",), "7-Zip archive"),
    ((".rar",), (b"Rar!\x1a\x07",), "RAR archive"),
    ((".gz", ".tgz"), (b"\x1f\x8b",), "gzip stream"),
    ((".msg",), (b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1",), "Outlook message"),
    ((".rtf",), (b"{\\rtf",), "RTF document"),
)

# A tar header carries its marker 257 bytes in, so it cannot be a leading magic.
_TAR_OFFSET = 257
_TAR_MARKER = b"ustar"

# AutoCAD writes the version at the very front ("AC1027" is R2013). Every
# version since R11 begins with these four bytes.
_DWG_PREFIX = b"AC10"

# ISO base media format: a "ftyp" box at offset 4, then the brand.
_FTYP = b"ftyp"

# An RFC 822 header block: a field name, a colon, and whatever follows. Used to
# confirm that a file called .eml is actually a message rather than a payload
# someone hoped the mail reader would process.
_MAIL_HEADER = re.compile(rb"(?m)^[A-Za-z][A-Za-z0-9-]{1,40}:")


def _header_mismatch(suffix: str, content: bytes) -> Optional[str]:
    """Name the format a suffix claims, when the bytes do not back it up.

    Returns None when the header is right (or when this suffix has no header
    rule). A suffix is only a claim; the bytes are the evidence. This is the
    same test the PDF and OpenXML branches already apply, extended to the
    formats the readers now accept.
    """
    for suffixes, magics, label in _CONTAINER_HEADERS:
        if suffix in suffixes:
            return None if content.startswith(magics) else label

    if suffix == ".tar":
        if content[_TAR_OFFSET:_TAR_OFFSET + len(_TAR_MARKER)] == _TAR_MARKER:
            return None
        return "tar archive"

    if suffix == ".dwg":
        return None if content.startswith(_DWG_PREFIX) else "DWG drawing"

    if suffix == ".dxf":
        # ASCII DXF opens with a SECTION group; binary DXF announces itself.
        head = content[:1024]
        if b"SECTION" in head or b"AutoCAD Binary DXF" in head:
            return None
        return "DXF drawing"

    if suffix in (".heic", ".heif", ".hif", ".avif"):
        if content[4:8] == _FTYP:
            return None
        return "HEIF image"

    if suffix == ".eml":
        return None if _MAIL_HEADER.search(content[:4096]) else "MIME message"

    return None


def encoded_size_exceeds_cap(encoded: str | None = None,
                             text: str | None = None) -> bool:
    """True when a payload is over the cap before it has to be materialised.

    base64 expands three bytes into four characters, so the decoded size is
    known from the length alone. Decoding an oversized payload and rejecting it
    afterwards spends exactly the memory the cap exists to protect: a 200 MB
    body would be fully allocated before anyone looked at it.

    `text` is measured in characters, which is a lower bound on the utf-8 byte
    length, so only certainly-oversized text is rejected here. The byte-level
    check in `verify_file_safety` still runs on whatever survives.
    """
    if encoded:
        return len(encoded) // 4 * 3 > MAX_ATTACHMENT_SIZE
    if text is not None:
        return len(text) > MAX_ATTACHMENT_SIZE
    return False


def verify_file_safety(filename: str, content: bytes) -> tuple[bool, str]:
    """Inspect an attachment for malicious patterns, extension spoofing, and magic bytes.

    Returns:
        (is_safe: bool, reason: str)
    """
    if not filename:
        return False, "Empty filename"

    clean_name = filename.strip()

    # 1. Size sanity check
    if len(content) > MAX_ATTACHMENT_SIZE:
        return False, f"Attachment exceeds maximum allowed size ({len(content) / (1024 * 1024):.1f}MB > 30MB)"

    lower_name = clean_name.lower()
    suffix = Path(clean_name).suffix.lower()

    # 2. Double-extension spoofing: a dangerous label directly before the final
    #    extension, e.g. "Draft_BL.exe.pdf". Only the second-to-last label is
    #    examined. Scanning every label flagged the ".com" of a domain name in
    #    "report.vendor.com.pdf" and a stray ".bin" in "booking.bin.BL.pdf",
    #    which are ordinary documents.
    parts = lower_name.split(".")
    if len(parts) >= 3 and f".{parts[-2]}" in EXECUTABLE_EXTENSIONS:
        return False, (f"Double-extension spoofing detected: '{clean_name}' "
                       f"(a '{parts[-2]}' label before '.{parts[-1]}')")

    # 3. Direct dangerous extension check
    if suffix in DANGEROUS_EXTENSIONS:
        return False, f"Executable or script extension blocked: '{suffix}'"

    # 4. Binary Magic-Byte inspection
    # 4a. Windows Portable Executable (PE: .exe, .dll, .scr, etc.)
    if content.startswith(b"MZ"):
        return False, f"Dangerous executable header (Windows PE / MZ) detected in '{clean_name}'"

    # 4b. Linux / Unix ELF executable
    if content.startswith(b"\x7fELF"):
        return False, f"Dangerous executable header (Linux ELF) detected in '{clean_name}'"

    # 4c. Java Class / Mach-O Fat Binary
    if content.startswith(b"\xca\xfe\xba\xbe"):
        return False, f"Dangerous compiled binary header (Mach-O / Java Class) detected in '{clean_name}'"

    # 5. Document container header verification
    if suffix == ".pdf":
        # Valid PDF must contain %PDF- in the first 1024 bytes
        if b"%PDF-" not in content[:1024]:
            return False, f"Spoofed or corrupted PDF header: '{clean_name}'"

    if suffix in (".docx", ".xlsx"):
        # Modern Office OpenXML files are ZIP archives starting with PK\x03\x04
        if not content.startswith(b"PK\x03\x04"):
            return False, f"Spoofed or corrupted Office OpenXML container: '{clean_name}'"

    mismatched = _header_mismatch(suffix, content)
    if mismatched:
        return False, f"Spoofed or corrupted {mismatched} header: '{clean_name}'"

    return True, "SAFE"
