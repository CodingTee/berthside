"""UN/EDIFACT and ANSI X12 messages as readable text.

An EDI message is the shipping instruction in its most structured form: the
carrier's system emits ``NAD+SH+SGSHP:172:20+SINGAPORE SHIPPING PTE LTD'`` and
the same seven facts a PDF prints in a table. Nothing here could read it, so an
``.edi`` attachment was classified by name and then escalated as unreadable.

Both syntaxes are mapped onto the *printed* labels the extractor already
recognises ("Port of Loading", "Gross Weight (KG)"), so no extraction rule has
to know that EDI exists. Weights are normalised to kilograms, because an EDI
message states its unit and a pound value compared against a kilogram value
would look like a real discrepancy.

Two deliberate restraints:

* **Only well-defined qualifiers are mapped.** Every party, location and
  measurement code in these syntaxes is a small dictionary, and the dictionaries
  are not all memorisable. Where a code is not certain the field is left
  missing and the code is reported in ``notes``. Printing a port the sender
  never gave is worse than printing nothing: it manufactures a discrepancy.
* **Reading never raises.** A malformed message returns ``None`` and the caller
  escalates it honestly.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Optional

log = logging.getLogger(__name__)

EDI_SUFFIXES = (".edi", ".edifact", ".x12", ".edifile", ".ifcsum", ".iftmin")

# EDIFACT interchange, message and group headers, and the X12 interchange
# header. Any one at the start of the file is proof of the syntax regardless of
# the extension. UNH is listed because feeds split out of a batch routinely drop
# the UNB envelope and keep only the message.
_EDIFACT_HEAD = ("UNA", "UNB", "UNG", "UNH")
_X12_HEAD = "ISA"

# Fallback for a message whose envelope was stripped: three or more segments at
# the start of a line, each a three letter tag followed by the element
# separator. A carrier's message arrives as .txt as often as .edi, and the
# structure is the only thing left to go on.
_EDIFACT_SNIFF = re.compile(r"(?m)^[A-Z][A-Z0-9]{2}\+.*$")
_EDIFACT_SNIFF_MIN = 3

MAX_TEXT_CHARS = 100_000

# --------------------------------------------------------------------- labels
# Print forms, chosen to match extractor.LABELS exactly.
LINK = "Port of Loading"
DLOAD = "Port of Discharge"
SHIPPER = "Shipper"
CONSIGNEE = "Consignee"
NOTIFY = "Notify Party"
CONTAINERS = "Container Count"
GROSS = "Gross Weight (KG)"
ETD = "ETD"
ETA = "ETA"

# Mass units these messages use, and how many kilograms one is.
_UNIT_TO_KG = {
    "KGM": 1.0, "KG": 1.0, "KGS": 1.0,
    "GRM": 0.001, "G": 0.001, "GRM ": 0.001,
    "TNE": 1000.0, "TON": 1000.0, "T": 1000.0,
    "LBR": 0.45359237, "LB": 0.45359237, "LBS": 0.45359237,
    "STN": 907.18474,
}
_MASS_UNITS = frozenset(_UNIT_TO_KG)

# A UN/LOCODE is five letters: two for the country, three for the place.
_LOCODE = re.compile(r"^[A-Z]{5}$")
_NUMBER = re.compile(r"^-?[\d.,]+$")

# Measurement dimensions that are a weight. A MEA segment is only read as a
# weight when its purpose is "WT" or its dimension is one of these, so a volume
# (AAW, in cubic metres) is never mistaken for a mass.
_WEIGHT_DIMENSIONS = frozenset({"AAD", "AAJ", "AAK", "G", "GROSS"})


@dataclass
class Parsed:
    """What a message yielded, plus what it could not be read as."""

    lines: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    seen: list[str] = field(default_factory=list)
    # True when the sender put the document itself into FTX free text. The
    # caller uses it to explain a message whose only content was prose.
    has_free_text: bool = False

    def add(self, label: str, value: str) -> None:
        if value:
            self.lines.append(f"{label} | {value}")

    def add_party(self, label: str, name: str, address: str = "") -> None:
        """A party as the extractor expects it: name on the label line.

        ``extractor.NAME_FIELDS`` takes the first line as the name and treats the
        following line as the street address, so the address goes underneath
        rather than on the same line.
        """
        if not name and not address:
            return
        self.lines.append(f"{label} | {name or address}")
        if name and address:
            self.lines.append(address)


# ------------------------------------------------------------------- edifact
def _edifact_delimiters(text: str) -> tuple[str, str, str, str, str]:
    """(component, element, decimal, release, terminator) for this message."""
    if text.startswith("UNA") and len(text) >= 9:
        return text[3], text[4], text[5], text[6], text[8]
    return ":", "+", ".", "?", "'"


def _edifact_segments(text: str) -> list[list[list[str]]]:
    """Split into segments of elements of components.

    The release character is what makes this a scan rather than a split: a
    delimiter preceded by it is content, not structure, and splitting naively
    mangles any address or name that contains a ``+`` or a ``?``.
    """
    component, element, _, release, terminator = _edifact_delimiters(text)
    if text.startswith("UNA") and len(text) >= 9:
        text = text[9:]

    segments: list[list[list[str]]] = []
    elements: list[list[str]] = []
    components: list[str] = []
    current: list[str] = []
    escaped = False

    for char in text:
        if escaped:
            current.append(char)
            escaped = False
            continue
        if char == release:
            escaped = True
            continue
        if char == terminator:
            components.append("".join(current))
            elements.append(components)
            segments.append(elements)
            current, components, elements = [], [], []
            continue
        if char == element:
            components.append("".join(current))
            elements.append(components)
            current, components = [], []
            continue
        if char == component:
            components.append("".join(current))
            current = []
            continue
        if char in "\r\n":
            continue
        current.append(char)

    if current or components or elements:
        components.append("".join(current))
        elements.append(components)
        segments.append(elements)
    return segments


def _element(elements: list[list[str]], index: int) -> list[str]:
    return elements[index] if 0 <= index < len(elements) else []


def _component(elements: list[list[str]], index: int, part: int = 0) -> str:
    parts = _element(elements, index)
    return parts[part].strip() if 0 <= part < len(parts) else ""


def _joined(elements: list[list[str]], index: int) -> str:
    return " ".join(p.strip() for p in _element(elements, index) if p and p.strip())


def _date(value: str, fmt: str) -> str:
    """A DTM value in the one shape a reader can compare."""
    digits = re.sub(r"\D", "", value)
    fmt = (fmt or "").strip()
    if fmt == "102" and len(digits) >= 8:
        return f"{digits[:4]}-{digits[4:6]}-{digits[6:8]}"
    if fmt == "101" and len(digits) >= 6:
        year = int(digits[:2])
        century = 1900 if year >= 70 else 2000
        return f"{century + year}-{digits[2:4]}-{digits[4:6]}"
    if fmt in ("203", "204") and len(digits) >= 8:
        return f"{digits[:4]}-{digits[4:6]}-{digits[6:8]}"
    if fmt == "718" and len(digits) >= 8:
        return f"{digits[:4]}-{digits[4:6]}-{digits[6:8]}"
    return value.strip()


def _place_name(elements: list[list[str]]) -> str:
    """``NAME (CODE)`` from a C517 location composite.

    The composite carries the place name and its UN/LOCODE in whichever order
    the sender's software emitted them, so both are picked out by shape rather
    than by position.
    """
    parts = [p.strip() for p in _element(elements, 2)
             if p and p.strip() and not _NUMBER.match(p.strip())]
    code = next((p for p in parts if _LOCODE.match(p.upper())), "")
    name = next((p for p in parts if not _LOCODE.match(p.upper())), "")
    if name and code:
        return f"{name} ({code.upper()})"
    return name or code.upper()


def _measure(elements: list[list[str]]) -> Optional[tuple[float, str]]:
    """``(kilograms, unit)`` from a MEA segment, or None when unreadable.

    The unit and the value sit in the same composite, in an order that varies,
    so each component is classified by what it looks like.
    """
    unit = ""
    value = ""
    for index in range(3, min(len(elements), 6)):
        for part in _element(elements, index):
            token = part.strip().upper()
            if not token:
                continue
            if not unit and token in _MASS_UNITS:
                unit = token
                continue
            if not value and _NUMBER.match(token):
                value = token
    if not unit or not value:
        return None
    try:
        number = float(value.replace(",", ""))
    except ValueError:
        return None
    return number * _UNIT_TO_KG[unit], unit


def _ftx_lines(elements: list[list[str]]) -> list[str]:
    """The free text an FTX segment carries, one line per element it used.

    Plenty of carriers send the whole document as FTX free text instead of
    structured segments: the sender prints their own document and wraps each
    line in ``FTX+AAA+++``. Those lines are already written the way the
    extractor reads labels, so they are passed through rather than mapped.

    The element the text sits in varies (4440, C107, C108), so every candidate
    is tried. Components are rejoined with the separator they were split on,
    which is the exact inverse of the split, and components that merely repeat
    a code are dropped by the alphabetic test.
    """
    out: list[str] = []
    for index in range(3, min(len(elements), 7)):
        parts = [p.strip() for p in _element(elements, index)]
        while parts and not parts[-1]:
            parts.pop()
        text = ":".join(p for p in parts if p)
        text = " ".join(text.split())
        if len(text) >= 3 and any(c.isalpha() for c in text):
            out.append(text)
    return out


def _parse_edifact(text: str) -> Parsed:
    result = Parsed()
    segments = _edifact_segments(text)

    containers = 0
    packages = ""
    unmapped: list[str] = []
    seen: list[str] = []

    for elements in segments:
        if not elements or not elements[0]:
            continue
        tag = (elements[0][0] or "").strip().upper()

        if tag == "NAD":
            qualifier = _component(elements, 1).upper()
            seen.append(f"NAD+{qualifier}")
            target = {"SH": SHIPPER, "CN": CONSIGNEE, "NI": NOTIFY,
                      "N1": NOTIFY, "CZ": NOTIFY}.get(qualifier)
            if target is None:
                unmapped.append(f"NAD+{qualifier}")
                continue
            # Elements 3 onward carry the name and then the address. Index 2 is
            # the party *code*, not its name, so it is only used when the
            # message gives nothing else.
            candidates = [_joined(elements, i) for i in range(3, min(len(elements), 10))]
            candidates = [c for c in candidates if c]
            if candidates:
                name = candidates[0]
                address = " ".join(c for c in candidates[1:] if c != name)
            else:
                name = _component(elements, 2)
                address = ""
            result.add_party(target, name, address)

        elif tag == "LOC":
            qualifier = _component(elements, 1).upper()
            seen.append(f"LOC+{qualifier}")
            target = {"9": LINK, "11": DLOAD, "7": DLOAD, "8": DLOAD,
                      "12": DLOAD, "88": LINK}.get(qualifier)
            if target is None:
                unmapped.append(f"LOC+{qualifier}")
                continue
            result.add(target, _place_name(elements))

        elif tag == "MEA":
            purpose = _component(elements, 1).upper()
            dim = _component(elements, 2).upper()
            seen.append(f"MEA+{purpose}+{dim}")
            # A measurement that is not a weight is a volume, a count or a
            # temperature: legitimately skipped, not an unmapped code.
            if purpose != "WT" and dim not in _WEIGHT_DIMENSIONS:
                continue
            got = _measure(elements)
            if got is None:
                result.notes.append(
                    f"MEA+{purpose}+{dim}: no mass value and unit could be read")
                continue
            kilos, unit = got
            result.add(GROSS, f"{kilos:,.2f} KG")
            if unit not in ("KGM", "KG"):
                result.notes.append(f"weight converted from {unit} to KG")

        elif tag == "EQD":
            qualifier = _component(elements, 1).upper()
            seen.append(f"EQD+{qualifier}")
            if qualifier == "CN":
                containers += 1

        elif tag == "GID":
            # C213: number and type of packages.
            quantity = _component(elements, 2)
            if quantity and not packages:
                packages = quantity

        elif tag == "DTM":
            # C507 packs the qualifier, the value and the format into one
            # element: DTM+133:20260320:102.
            qualifier = _component(elements, 1, 0).upper()
            seen.append(f"DTM+{qualifier}")
            target = {"133": ETD, "137": "", "178": ETA, "186": ETA}.get(qualifier)
            if not target:
                continue
            value = _component(elements, 1, 1)
            fmt = _component(elements, 1, 2)
            result.add(target, _date(value, fmt))

        elif tag == "FTX":
            # Free text: either prose about the shipment, or the whole document
            # when the sender wrapped their own layout. Appended verbatim
            # because the lines already carry their own labels.
            seen.append("FTX")
            free = _ftx_lines(elements)
            if free:
                result.lines.extend(free)
                result.has_free_text = True

        elif tag in ("TDT", "RFF", "CNT", "PCI"):
            # Transport, references and counts: present in the message but
            # never one of the compared fields.
            seen.append(tag)

    if containers:
        result.add(CONTAINERS, str(containers))
    elif packages:
        # No equipment segment, so the package count is the only quantity the
        # message states. Reported as the container count with a note, because
        # for a groupage shipment the two are the same number and a missing
        # count reads as a discrepancy.
        result.add(CONTAINERS, packages)
        result.notes.append(
            "no EQD segments: the GID package quantity was used as the container count")

    if unmapped:
        result.notes.append(
            "qualifiers not mapped, so those fields stay missing: "
            + ", ".join(sorted(set(unmapped))))
    result.seen = seen
    return result


# ----------------------------------------------------------------------- x12
# X12 qualifiers, from the load tender (204) and shipment status (214) sets.
_X12_PARTY = {"SH": SHIPPER, "CN": CONSIGNEE, "NI": NOTIFY, "N1": NOTIFY}
# The first element of an R4 is the port function. Only the two slots every
# carrier set uses are mapped; anything else is reported and left missing,
# because guessing would print a port the sender never gave.
_X12_PORT_FUNCTION = {"1": LINK, "2": DLOAD}
_X12_DATETIME = {"133": ETD, "178": ETA, "186": ETA}


def _x12_delimiters(text: str) -> tuple[str, str, str]:
    """``(element, terminator, sub-element)`` for this interchange.

    A full interchange states them positionally in the fixed width ISA header.
    The envelope is routinely stripped though (a single transaction set pulled
    out of a batch arrives as ``ST*404*0001~...``), and the old positional read
    then took the delimiters from arbitrary characters: a real message parsed to
    zero lines. When there is no ISA they are inferred from the first segment
    instead, which is unambiguous because the first segment is always a tag.
    """
    if text.startswith(_X12_HEAD) and len(text) > 106:
        element = text[3] if text[3].strip() else "*"
        sub = text[104] if text[104].strip() else ":"
        terminator = text[105] if text[105].strip() else "~"
        return element, terminator, sub

    # Newlines are not delimiters, so the first segment can be read on its own
    # line: tag, then the element separator.
    body = re.sub(r"[\r\n]", "", text)
    found = re.match(r"[A-Z0-9]{2,3}([^\sA-Za-z0-9])", body)
    element = found.group(1) if found else "*"
    # The terminator is whatever closes a segment, so it is the character that
    # sits immediately before the next tag and its element separator.
    closing = re.search(r"([^\sA-Za-z0-9])[A-Z0-9]{2,3}" + re.escape(element), body)
    terminator = closing.group(1) if closing else "~"
    if terminator == element:
        terminator = "~"
    return element, terminator, ":"


def _x12_segments(text: str) -> tuple[list[list[str]], str, str]:
    element, terminator, sub = _x12_delimiters(text)
    if terminator in text:
        # A terminator makes the interchange a character stream. Any newline in
        # it is the sender's line wrapping, so it carries no structure and
        # would otherwise be read as part of a value.
        body = text.replace("\r\n", "").replace("\n", "")
        chunks = body.split(terminator)
    else:
        # No terminator survived, which is what a single transaction set lifted
        # out of a batch looks like. One segment per line is then the only
        # boundary the message states, and treating the whole file as one
        # segment parses it to nothing.
        chunks = re.split(r"[\r\n]+", text)
    segments = [[part.strip() for part in chunk.split(element)] for chunk in chunks]
    return [s for s in segments if s and s[0]], element, sub


def _x12_place(elements: list[str]) -> str:
    """``NAME (CODE)`` from an R4 port segment.

    Element 2 is the location *qualifier* ("K" for a port), not part of the
    name, so the search starts after it.
    """
    parts = [p.strip() for p in elements[3:]
             if p and p.strip() and not _NUMBER.match(p.strip())]
    code = next((p for p in parts if _LOCODE.match(p.upper())), "")
    name = next((p for p in parts if not _LOCODE.match(p.upper())), "")
    if name and code:
        return f"{name} ({code.upper()})"
    return name or code.upper()


def _x12_weight(elements: list[str]) -> Optional[tuple[float, str]]:
    """``(kilograms, unit)`` from a MEA segment: ``MEA*WT*G*24960*KG``."""
    if len(elements) < 3 or (elements[1] or "").upper() not in ("WT", "AAE"):
        return None
    qualifier = (elements[2] or "").upper()
    if qualifier not in _WEIGHT_DIMENSIONS:
        return None
    value = ""
    unit = ""
    for token in elements[3:]:
        token = (token or "").strip().upper()
        if not token:
            continue
        if not unit and token in _MASS_UNITS:
            unit = token
        elif not value and _NUMBER.match(token):
            value = token
    if not value:
        return None
    # A message that states no unit is read as kilograms, which is what the
    # corpus documents print and what the field is named after.
    unit = unit or "KGM"
    try:
        return float(value.replace(",", "")) * _UNIT_TO_KG[unit], unit
    except ValueError:
        return None


def _x12_notes(elements: list[str]) -> list[str]:
    """The free-form text an NTE segment carries, one line per text element.

    ``NTE*GEN*<line>`` is the layout every sender uses: the note reference code
    is element 1 and the text starts at element 2. The specification allows more
    than one text element per segment, and each one is a separate line, so they
    are not joined back together.
    """
    out: list[str] = []
    for raw in elements[2:]:
        text = " ".join((raw or "").split())
        if len(text) >= 2 and any(c.isalpha() for c in text):
            out.append(text)
    return out


def _parse_x12(text: str) -> Parsed:
    result = Parsed()
    segments, _element, _ = _x12_segments(text)

    containers = 0
    packages = ""
    current_party = ""
    address_for: dict[str, list[str]] = {}
    unmapped: list[str] = []

    for elements in segments:
        tag = (elements[0] or "").strip().upper()

        if tag == "N1":
            qualifier = (elements[1] if len(elements) > 1 else "").strip().upper()
            target = _X12_PARTY.get(qualifier)
            if target is None:
                unmapped.append(f"N1*{qualifier}")
                current_party = ""
                continue
            current_party = target
            name = elements[2] if len(elements) > 2 else ""
            result.add_party(target, name.strip())
            address_for.setdefault(target, [])

        elif tag == "N3":
            if current_party:
                address_for.setdefault(current_party, []).append(
                    (elements[1] if len(elements) > 1 else "").strip())

        elif tag == "N4":
            if current_party:
                parts = [p.strip() for p in elements[1:] if p and p.strip()]
                if parts:
                    address_for.setdefault(current_party, []).append(", ".join(parts))

        elif tag == "N7":
            # Equipment details: one segment per container.
            containers += 1

        elif tag == "TD1":
            # TD102 is the lading quantity. Used only when no equipment segment
            # states the count directly.
            if len(elements) > 2 and _NUMBER.match((elements[2] or "").strip()):
                packages = packages or elements[2].strip()

        elif tag == "MEA":
            got = _x12_weight(elements)
            if got is None:
                unmapped.append("MEA*" + "*".join(elements[1:3]))
                continue
            kilos, unit = got
            result.add(GROSS, f"{kilos:,.2f} KG")
            if unit not in ("KGM", "KG"):
                result.notes.append(f"weight converted from {unit} to KG")

        elif tag == "DTM":
            qualifier = (elements[1] if len(elements) > 1 else "").strip().upper()
            target = _X12_DATETIME.get(qualifier)
            if target is None:
                unmapped.append(f"DTM*{qualifier}")
                continue
            value = elements[2] if len(elements) > 2 else ""
            fmt = (elements[3] if len(elements) > 3 else "").strip()
            result.add(target, _date(value, fmt or ("102" if len(re.sub(r"\D", "", value)) >= 8 else "")))

        elif tag == "NTE":
            # Free-form notes. Like EDIFACT FTX, plenty of senders put the whole
            # document here, one NTE per line, because it is the only segment
            # that carries unconstrained text.
            free = _x12_notes(elements)
            if free:
                result.lines.extend(free)
                result.has_free_text = True

        elif tag == "R4":
            function = (elements[1] if len(elements) > 1 else "").strip()
            target = _X12_PORT_FUNCTION.get(function)
            if target is None:
                unmapped.append(f"R4*{function}")
                continue
            result.add(target, _x12_place(elements))

    if containers:
        result.add(CONTAINERS, str(containers))
    elif packages:
        result.add(CONTAINERS, packages)
        result.notes.append(
            "no N7 equipment segments: the TD1 lading quantity was used as the "
            "container count")

    # Addresses go under their party, which is where the extractor reads them.
    if address_for:
        merged: list[str] = []
        for line in result.lines:
            merged.append(line)
            if line.startswith(f"{SHIPPER} | "):
                merged.extend(address_for.get(SHIPPER, []))
            elif line.startswith(f"{CONSIGNEE} | "):
                merged.extend(address_for.get(CONSIGNEE, []))
            elif line.startswith(f"{NOTIFY} | "):
                merged.extend(address_for.get(NOTIFY, []))
        result.lines = merged
        for target, lines in address_for.items():
            if lines and not any(line.startswith(f"{target} | ") for line in result.lines):
                result.add_party(target, "", " ".join(lines))

    if unmapped:
        result.notes.append(
            "qualifiers not mapped, so those fields stay missing: "
            + ", ".join(sorted(set(unmapped))))
    result.seen = [s[0] for s in segments]
    return result


# --------------------------------------------------------------------- public
def detect_edi(filename: str, content: Optional[bytes] = None) -> Optional[str]:
    """``"edifact"``, ``"x12"`` or None.

    The header decides, not the extension: a carrier's message arrives as
    ``.txt`` as often as ``.edi``, and ``UNA``/``UNB``/``ISA`` in the first
    bytes is proof of what it is. When the envelope was stripped, a run of
    well formed segments is the next best evidence, and the extension is the
    last resort.
    """
    head = bytes(content or b"")[:110]
    if head.startswith(b"ISA"):
        return "x12"
    for marker in _EDIFACT_HEAD:
        if head.startswith(marker.encode()):
            return "edifact"
    name = str(filename or "").lower()
    if name.endswith(".x12"):
        return "x12"
    if content is not None:
        # Decoded the same way the parsers decode, so a byte that is not valid
        # UTF-8 cannot turn a real message into an undecodable one here.
        window = _decode_message(bytes(content)[:4096])
        if len(_EDIFACT_SNIFF.findall(window)) >= _EDIFACT_SNIFF_MIN:
            return "edifact"
    if any(name.endswith(s) for s in EDI_SUFFIXES):
        return "edifact"
    return None


def is_edi(filename: str, content: Optional[bytes] = None) -> bool:
    return detect_edi(filename, content) is not None


def read_edi_text(filename: str, content: bytes) -> Optional[tuple[str, str]]:
    """Read a message, returning ``(text, source)`` or None.

    ``notes`` are not returned: they belong with the document's processing
    record, and the caller reads them from :func:`parse_edi` when it wants them.
    """
    parsed = parse_edi(filename, content)
    if parsed is None or not parsed.lines:
        return None
    syntax = detect_edi(filename, content) or "edifact"
    text = "\n".join(parsed.lines)[:MAX_TEXT_CHARS]
    return text, f"edi-{syntax}"


def _decode_message(content: bytes) -> str:
    """Decode a message without ever raising and without losing ASCII.

    UTF-8 comes first because that is what modern feeds send: reading their
    bytes as latin-1 turns ``毛重`` into ``æ¯é``, which hides any label written
    outside ASCII. The strict attempt means a legacy single-byte message still
    falls through to cp1252 intact rather than being half-mangled, and latin-1
    is the backstop because it cannot fail.
    """
    raw = bytes(content or b"")
    for codec in ("utf-8", "cp1252"):
        try:
            return raw.decode(codec)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("latin-1", errors="replace")


def parse_edi(filename: str, content: bytes) -> Optional[Parsed]:
    """Full result, including the notes and the segments that were seen."""
    syntax = detect_edi(filename, content)
    if syntax is None:
        return None
    try:
        text = _decode_message(content)
        return _parse_edifact(text) if syntax == "edifact" else _parse_x12(text)
    except Exception as exc:  # noqa: BLE001 - never let a message break ingest
        log.info("EDI reader failed for %s (%s): %s", filename, syntax, exc)
        return None
