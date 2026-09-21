"""Email utility helpers for normalization, prefix cleaning, and thread preservation."""
from __future__ import annotations

import re
from typing import Optional

# Regex for common email forwarding and reply prefixes across languages (en, zh, de, fr, es)
_FWD_RE_PREFIX = re.compile(
    r"^(?:\[?(?:fwd?|re|fw|aw|wg|rv|res|转发|回復|回复)\]?[\s:：\-_]*)+",
    re.IGNORECASE,
)

# Suffixes added during earlier audit or dispatch iterations
_AUDIT_SUFFIX = re.compile(
    r"\s*-\s*(?:Document Verification Complete|B/L Document Amendment Required|Document Ingestion Rejected|Discrepancy Notice).*$",
    re.IGNORECASE,
)

# Regex to detect original forwarder/customer in forwarded message headers in email body
_ORIGINAL_SENDER_RE = re.compile(
    r"(?:(?:From|发件人|De|Von|De\s+la\s+part\s+de)[\s:：]+(?:[^\n<]*<)?([a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+)>?)",
    re.IGNORECASE,
)


def clean_subject(subject: Optional[str]) -> str:
    """Normalize subject line by stripping all cascaded Fwd:, Re:, FW: prefixes.

    Prevents ugly double or triple prefixes like 'Fwd: Fwd: ...' or 'Re: Fwd: ...'
    when emails are forwarded between clients, forwarders, and the automated hub.
    """
    if not subject:
        return ""
    text = subject.strip()
    # Strip any trailing audit suffixes from previous rounds
    text = _AUDIT_SUFFIX.sub("", text).strip()
    # Strip leading Fwd:, Re:, FW:, 转发:, etc. recursively
    while True:
        cleaned = _FWD_RE_PREFIX.sub("", text).strip()
        if cleaned == text:
            break
        text = cleaned
    return text or subject.strip()


def extract_original_sender(body: Optional[str]) -> Optional[str]:
    """Detect the original client/shipper email from forwarded email headers in body."""
    if not body:
        return None
    matches = _ORIGINAL_SENDER_RE.findall(body)
    if matches:
        for m in matches:
            if m and "@" in m:
                return m.strip()
    return None
