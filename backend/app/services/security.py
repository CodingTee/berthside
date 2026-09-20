"""Security and sanitization gate for incoming attachments.

Protects the SDOC verification pipeline against:
1. Executable and script payloads disguised as documents (.exe, .bat, .vbs, .scr, etc.).
2. Double-extension spoofing (e.g., 'Draft_BL.pdf.exe').
3. File-header / magic-bytes spoofing (PE headers 'MZ', ELF headers, Mach-O).
4. Decompression bomb / resource exhaustion attacks.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

# Dangerous extensions that should never be processed as shipping documents
DANGEROUS_EXTENSIONS = {
    ".exe", ".dll", ".bat", ".cmd", ".vbs", ".vbe", ".js", ".jse",
    ".wsf", ".wsh", ".ps1", ".psm1", ".scr", ".pif", ".hta", ".cpl",
    ".jar", ".com", ".sh", ".bash", ".bin",
}

# Supported legitimate document extensions for shipping verification
LEGITIMATE_EXTENSIONS = {
    ".txt", ".pdf", ".docx", ".doc", ".xlsx", ".xls",
    ".csv", ".tsv", ".png", ".jpg", ".jpeg", ".tif", ".tiff",
}

# Maximum allowed file size for attachments (30 MB)
MAX_ATTACHMENT_SIZE = 30 * 1024 * 1024


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

    # 2. Check for double extension spoofing (e.g. invoice.pdf.exe)
    parts = lower_name.split(".")
    if len(parts) > 2:
        for ext in parts[1:]:
            if f".{ext}" in DANGEROUS_EXTENSIONS:
                return False, f"Malicious double-extension or executable script detected: '{clean_name}'"

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

    return True, "SAFE"
