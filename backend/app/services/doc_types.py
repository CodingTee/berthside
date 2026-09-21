"""One canonical interpretation of a shipping-document filename.

Why this module exists
----------------------
The Gmail layer and the verification engine each grew their own idea of what an
SI or a BL is. The engine accepted fuzzy, human names ("Shipping_Instruction_
PO123.pdf", "Draft_BL_v2.xlsx"); the Gmail layer demanded the corpus convention
`_SI.`/`_BL.` immediately before the extension. Those two disagreed in the worst
possible way: for a mail carrying `SI_v1.pdf`, the Gmail layer did not recognise
the attachment as an SI, synthesised a document out of the **email body**, and
the engine then preferred that synthetic file over the real PDF — so the attached
document was never read at all.

This module is the single answer to "what kind of document is this file?".
Both layers call it, so a name cannot be a document in one place and not in the
other.

What it does NOT do: read file contents. It is a naming classifier only, and it
returns ``None`` rather than guessing whenever a name is silent or ambiguous. The
content-based judgement stays where it has always been — in the engine's
document readers and extractor.
"""
from __future__ import annotations

import re
from typing import Optional

# Extensions the engine can actually attempt to read. A name ending in anything
# else is not treated as a document at all, so an unrelated attachment is never
# bound to one side of a comparison.
DOC_SUFFIXES = frozenset({
    "txt", "pdf", "docx", "xlsx", "doc", "xls", "csv", "rtf",
    "png", "jpg", "jpeg", "tif", "tiff", "msg", "eml",
    # Structured exports and the other office containers a forwarder sends.
    "json", "jsonl", "ndjson", "xml",
    "odt", "ods", "odp", "ott", "ots", "otp",
    "pages", "numbers", "key",
    # Carrier messages and drawings.
    "edi", "edifact", "x12", "edifile", "ifcsum", "iftmin", "dxf", "dwg",
    # Phone photographs of a stamped document.
    "heic", "heif", "hif", "avif", "bmp", "webp",
})

# A type is claimed either by a whole token in the name ("Draft_BL_v2") or by a
# phrase that survives separator removal ("Shipping_Instruction_PO123"). The bare
# words "shipping" and "draft" are deliberately NOT here: they turn up in
# unrelated names ("shipping_invoice.pdf" is an invoice, not an SI).
SI_BL_TOKENS = {
    "SI": frozenset({"si"}),
    "BL": frozenset({"bl", "bol", "hbl", "mbl"}),
}
SI_BL_PHRASES = {
    "SI": ("shippinginstruction", "siform"),
    "BL": ("billoflading", "draftbl", "bldraft", "blcopy"),
}

# The corpus naming convention: "<id>_SI.pdf". Kept because it is the strongest
# possible signal when present.
CONVENTION_RE = re.compile(r"_(SI|BL)\.[A-Za-z0-9]+$", re.IGNORECASE)

# A commercial invoice. Matched separately because it is never compared against
# the SI/BL pair — it only has to be recognised so the shipment can report it as
# present.
INVOICE_RE = re.compile(r"(invoice|inv[._-]?\d|commercial[._ -]?inv)", re.IGNORECASE)

# Marks a file this system generated from an email body rather than received as
# an attachment. Written into the filename so a synthetic document can never be
# mistaken for — or preferred over — a real one.
SYNTHETIC_SUFFIX = ".frombody"


def _stem_and_suffix(filename: str) -> tuple[str, str]:
    name, dot, suffix = str(filename or "").rpartition(".")
    if not dot or not name:
        return "", ""
    return name, suffix.lower()


def detect_si_bl(filename: str) -> Optional[str]:
    """Guess ``"SI"`` or ``"BL"`` from a filename, else ``None``.

    Returns None when the name is silent about the type, when it names both
    ("SI_and_BL.pdf"), or when the suffix is not a document type at all. An
    ambiguous attachment must never be bound to one side of a comparison.

    Also returns ``None`` for a synthetic body-derived document: it is a
    fallback, never a document in its own right, and the caller decides whether
    to use it only when nothing real is available.
    """
    name, suffix = _stem_and_suffix(filename)
    if not suffix or suffix not in DOC_SUFFIXES:
        return None
    stem = name.lower()
    tokens = set(re.split(r"[^a-z0-9]+", stem))
    joined = re.sub(r"[^a-z0-9]", "", stem)
    claimed = {
        doc_type
        for doc_type, names in SI_BL_TOKENS.items()
        if tokens & names
    }
    claimed |= {
        doc_type
        for doc_type, phrases in SI_BL_PHRASES.items()
        if any(phrase in joined for phrase in phrases)
    }
    # Exactly one claim, or nothing. A name that claims both — "SI_and_BL.pdf",
    # "SHP-001_SI_BL.pdf" — is ambiguous and must not be bound to one side of a
    # comparison, even when it also satisfies the corpus convention.
    if len(claimed) != 1:
        return None
    return claimed.pop()


def is_invoice(filename: str) -> bool:
    """True when the name identifies a commercial invoice."""
    return bool(INVOICE_RE.search(str(filename or "")))


def is_synthetic(filename: str) -> bool:
    """True for a document this system generated from an email body."""
    return str(filename or "").lower().endswith(SYNTHETIC_SUFFIX + ".txt") or \
        SYNTHETIC_SUFFIX in str(filename or "").lower()


def detect(filename: str) -> Optional[str]:
    """``"SI"``, ``"BL"``, ``"INVOICE"`` or ``None`` for a filename.

    SI/BL win over INVOICE: a name like ``SHP-001_SI_invoice.pdf`` that names
    both is ambiguous for the comparison, and treating it as an invoice would
    silently remove a compared document from the shipment. ``detect_si_bl``
    returning None for that name is what keeps it out of both buckets.
    """
    kind = detect_si_bl(filename)
    if kind is not None:
        return kind
    if is_invoice(filename):
        return "INVOICE"
    return None
