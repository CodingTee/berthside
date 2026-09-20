"""Business identifiers read out of a shipping document's own text.

Why this exists
---------------
Shipment grouping used to look only at the email: subject, body, attachment
filenames and even the internal ``email_id``. The document's own reference was
never consulted, and no reference field was extracted, so a mail whose only
booking number sits inside the PDF could not be grouped by it — and the internal
email id could be mistaken for a business reference.

This module reads the identifiers that a shipping document actually prints:

* a **business reference** — ``Booking No:``, ``OC No.:``, ``Shipment ID:``,
  ``B/L No.:``, ``S/O No.``, ``Reference:`` …;
* **ISO container numbers** (4 letters + 7 digits, e.g. ``ABCU1234567``);
* **vessel** and **voyage**.

Nothing here is a compared field: the 7-field comparison model is untouched, and
these are never added to ``extractor.LABELS``. They are *identity* signals for
grouping, which is why they live in their own module with their own tests.

Deliberately conservative: an identifier is returned only when a label or a
valid ISO-6346 shape is present, because a wrong identifier is worse than none —
it silently attaches a document to the wrong shipment.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

# "Booking No: ABC123" / "OC No.: 5RSG-00133" / "B/L No.: MEDUUD104332" /
# "Shipment ID: SHP-001" / "Reference: INV-1" / "S/O No. 12345".
# The label is required: without it any token in the document would qualify.
_REFERENCE_LABEL = re.compile(
    r"\b(?:booking|b\s*/\s*l|bl|bill\s+of\s+lading|s\s*/\s*o|shipping\s+order|"
    r"shipment(?:\s+id)?|oc|order\s+confirmation|job|reference|ref|"
    r"file|shipment\s+no)\s*"
    r"(?:no|number|#|num|id|ref)?\s*"
    # A label is often followed by more than one separator ("OC No.: 5RSG-00133").
    r"[:.#\-–]*\s*"
    # A reference may start with a digit ("OC No.: 5RSG-00133", "S/O No. 12345"),
    # so the value is alphanumeric; `_clean_reference` rejects anything that
    # carries no digit at all.
    r"([A-Z0-9][A-Z0-9]{1,19}(?:[-/_][A-Z0-9]{1,12}){0,2})",
    re.IGNORECASE,
)

# ISO 6346: three owner letters, a U/J/Z equipment category letter, seven digits.
_CONTAINER = re.compile(r"\b([A-Z]{3}[UJZ]\d{7})\b")

_VESSEL = re.compile(
    r"\b(?:vessel(?:\s+name)?|ship\s+name|m\.?v\.?|m\.?s\.?c\.?)\s*[:.]?\s*"
    r"([A-Z][A-Z0-9 .'&-]{2,40})",
    re.IGNORECASE,
)
# "Voy. No: 026E", "Voyage: 118W", "Vessel Voyage 257087E", "V.257087E".
_VOYAGE = re.compile(
    r"\b(?:voyage|voy\.?|vessel\s+voyage|v\.)\s*"
    r"(?:no|number|#)?\s*[:.]?\s*([A-Z0-9][A-Z0-9-]{1,11})\b",
    re.IGNORECASE,
)

# Values that look like a reference but are not one.
_NOT_A_REFERENCE = frozenset({
    "NO", "NUMBER", "REF", "REFERENCE", "TBA", "TBC", "TBD", "NA", "NIL", "NONE",
    "PENDING", "COPY", "DRAFT", "ORIGINAL", "PREPAID", "COLLECT", "FREIGHT",
})

# A vessel name captured past the end of its own line ("EVER GLORY V.026E").
_VESSEL_TAIL = re.compile(r"\s+(?:v\.?|voy|voyage)\b.*$", re.IGNORECASE)


@dataclass
class DocumentIdentifiers:
    """Identity signals found inside one document's text."""

    reference: Optional[str] = None
    containers: tuple[str, ...] = field(default_factory=tuple)
    vessel: Optional[str] = None
    voyage: Optional[str] = None

    @property
    def is_empty(self) -> bool:
        return not (self.reference or self.containers or self.vessel or self.voyage)

    def discriminators(self) -> tuple[str, ...]:
        """Signals that tell two shipments on the same route apart.

        Container numbers are the strongest: they are unique per physical
        movement. Vessel+voyage is the next best. Ordered so the signature is
        stable regardless of the order the text mentioned them.
        """
        parts: list[str] = []
        if self.containers:
            parts.append("CTR:" + ",".join(sorted(set(self.containers))))
        if self.vessel or self.voyage:
            parts.append("VES:{}:{}".format((self.vessel or "").upper(),
                                            (self.voyage or "").upper()))
        return tuple(parts)


def _clean_reference(value: str) -> Optional[str]:
    candidate = (value or "").strip().strip(".,;:").upper()
    if not candidate or candidate in _NOT_A_REFERENCE:
        return None
    # A label with no value ("Booking No:") must not become a reference, and a
    # bare word is not one either.
    if not any(ch.isdigit() for ch in candidate):
        return None
    if len(candidate) < 4:
        return None
    return candidate


def extract_reference(text: str) -> Optional[str]:
    """The first business reference the document prints, upper-cased."""
    for match in _REFERENCE_LABEL.finditer(str(text or "")):
        reference = _clean_reference(match.group(1))
        if reference:
            return reference
    return None


def extract_containers(text: str) -> tuple[str, ...]:
    """Every ISO container number in the text, in document order."""
    seen: list[str] = []
    for match in _CONTAINER.finditer(str(text or "").upper()):
        value = match.group(1)
        if value not in seen:
            seen.append(value)
    return tuple(seen)


def extract_vessel_voyage(text: str) -> tuple[Optional[str], Optional[str]]:
    """The vessel name and voyage number, when labelled as such."""
    text = str(text or "")
    vessel = None
    match = _VESSEL.search(text)
    if match:
        candidate = _VESSEL_TAIL.sub("", match.group(1)).strip(" .,;")
        if candidate and not candidate.isdigit() and len(candidate) >= 3:
            vessel = candidate
    voyage = None
    match = _VOYAGE.search(text)
    if match:
        candidate = match.group(1).strip(" .,;")
        if any(ch.isdigit() for ch in candidate):
            voyage = candidate
    return vessel, voyage


def from_text(text: str) -> DocumentIdentifiers:
    """All identity signals in one document's text."""
    vessel, voyage = extract_vessel_voyage(text)
    return DocumentIdentifiers(
        reference=extract_reference(text),
        containers=extract_containers(text),
        vessel=vessel,
        voyage=voyage,
    )


def merge(*sets_of: DocumentIdentifiers) -> DocumentIdentifiers:
    """Combine the identifiers of several documents of the same shipment."""
    reference = None
    containers: list[str] = []
    vessel = voyage = None
    for ids in sets_of:
        if ids is None:
            continue
        reference = reference or ids.reference
        for value in ids.containers:
            if value not in containers:
                containers.append(value)
        vessel = vessel or ids.vessel
        voyage = voyage or ids.voyage
    return DocumentIdentifiers(reference=reference, containers=tuple(containers),
                               vessel=vessel, voyage=voyage)
