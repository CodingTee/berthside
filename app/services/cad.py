"""CAD drawings as readable text (``.dxf`` and ``.dwg``).

A container stowage plan, a packing drawing and an equipment layout arrive as
CAD files, and the party names, ports and weights are printed on them as text
entities. Nothing here could read that, so the file was classified by name and
escalated as unreadable.

Two very different reads:

* **DXF** comes in an ASCII form that is a stream of ``code`` / ``value`` line
  pairs, and text entities are first-class: a ``TEXT`` or ``MTEXT`` holds its
  string in group code 1, and an ``ATTRIB`` holds a *tag* in group code 2 and
  its value in group code 1. An ``ATTRIB`` pair is literally ``label | value``,
  which is the shape the extractor reads, so a titled block comes out already
  aligned. Binary DXF is a different container and is refused honestly rather
  than decoded as text.
* **DWG** is Autodesk's closed binary format and no reader for it exists in
  pure Python. Its text entities are stored as plain strings inside the object
  stream, so printable runs are recovered and then filtered: a run only counts
  as a field when it carries a label the extractor recognises *and* the document
  as a whole yields at least :data:`MIN_FIELDS` of them. One stray word that
  happens to look like a label is not enough, because a wrong value reads as a
  discrepancy the customer never made.
"""
from __future__ import annotations

import logging
import re
from typing import Optional

log = logging.getLogger(__name__)

DXF_SUFFIX = ".dxf"
DWG_SUFFIX = ".dwg"
CAD_SUFFIXES = (DXF_SUFFIX, DWG_SUFFIX)

MAX_TEXT_CHARS = 100_000

# How many recognised fields a binary scavenge must find before its output is
# trusted. Two is the same bar the legacy .doc decoder is held to: below it, the
# evidence is a coincidence rather than a document.
MIN_FIELDS = 2

# ASCII DXF sentinel and the binary DXF marker.
_DXF_BINARY = b"AutoCAD Binary DXF"
_DWG_MAGIC = b"AC10"

# Labels, spelled the way the extractor reads them. A drawing prints "SHIPPER:"
# or "PORT OF LOADING" as a text entity, so each pattern is an anchored label
# followed by a separator and then the value.
_LABELED = (
    ("Shipper", re.compile(
        r"^\s*(?:SHIPPER|SHIP\s*FROM|EXPORTER|SHPR)\s*[:=\-]\s*(.+)$", re.IGNORECASE)),
    ("Consignee", re.compile(
        r"^\s*(?:CONSIGNEE|CNEE|CONSIGNED\s+TO|SHIP\s*TO)\s*[:=\-]\s*(.+)$", re.IGNORECASE)),
    ("Notify Party", re.compile(
        r"^\s*(?:NOTIFY(?:\s+PARTY)?|ALSO\s+NOTIFY)\s*[:=\-]\s*(.+)$", re.IGNORECASE)),
    ("Port of Loading", re.compile(
        r"^\s*(?:PORT\s+OF\s+LOADING|LOAD(?:ING)?\s+PORT|POL)\s*[:=\-]\s*(.+)$",
        re.IGNORECASE)),
    ("Port of Discharge", re.compile(
        r"^\s*(?:PORT\s+OF\s+DISCHARGE|DISCHARGE\s+PORT|POD|DESTINATION)\s*[:=\-]\s*(.+)$",
        re.IGNORECASE)),
    ("Container Count", re.compile(
        r"^\s*(?:CONTAINERS?|CONTAINER\s+COUNT|NO\.?\s+OF\s+CONTAINERS?|CTNS?)\s*[:=\-]\s*(.+)$",
        re.IGNORECASE)),
    # The weight row prints its unit: "GROSS WEIGHT (KG)". The unit belongs to
    # the label, so it is skipped before the separator.
    ("Gross Weight (KG)", re.compile(
        r"^\s*(?:GROSS\s+WEIGHT|GROSS\s+WT\.?|G\.?W\.?)\s*(?:\(\s*KGS?\s*\))?\s*[:=\-]\s*(.+)$",
        re.IGNORECASE)),
)

# MTEXT embeds its own formatting: \A1; a paragraph, \P a line break, {\fArial;…}
# a font run, and \~ a non-breaking space. Left in place they read as content.
_MTEXT_PARAGRAPH = re.compile(r"\\P", re.IGNORECASE)
_MTEXT_FORMAT = re.compile(r"\\[A-Za-z][^;\\]*;")
_MTEXT_BRACES = re.compile(r"[{}]")
_MTEXT_NBSP = re.compile(r"\\~")


def detect_cad(filename: str, content: Optional[bytes] = None) -> Optional[str]:
    """``"dxf"``, ``"dwg"`` or None."""
    head = bytes(content or b"")[:64]
    if head.startswith(_DWG_MAGIC):
        return "dwg"
    if head.startswith(_DXF_BINARY):
        return "dxf-binary"
    name = str(filename or "").lower()
    if name.endswith(DWG_SUFFIX):
        return "dwg"
    if name.endswith(DXF_SUFFIX):
        return "dxf"
    # An unnamed DXF announces itself with its first group code pair.
    if head.startswith(b"0\nSECTION") or head.startswith(b"  0\r\nSECTION"):
        return "dxf"
    return None


def is_cad(filename: str, content: Optional[bytes] = None) -> bool:
    return detect_cad(filename, content) is not None


def _is_ascii_dxf(content: bytes) -> bool:
    """ASCII DXF is text; a high proportion of control bytes means it is not."""
    sample = bytes(content[:4096])
    if not sample:
        return False
    printable = sum(1 for b in sample if 32 <= b < 127 or b in (9, 10, 13))
    return printable / len(sample) > 0.9


def _mtext_clean(value: str) -> str:
    value = _MTEXT_PARAGRAPH.sub("\n", value)
    value = _MTEXT_NBSP.sub(" ", value)
    value = _MTEXT_FORMAT.sub("", value)
    value = _MTEXT_BRACES.sub("", value)
    return value.strip()


def _dxf_pairs(content: bytes):
    """Yield ``(code, value)`` from an ASCII DXF.

    The format is two lines per pair: a group code, then its value. Anything
    that does not parse as a code ends the stream, which is what a truncated
    file looks like.
    """
    text = content.decode("latin-1", errors="replace")
    lines = text.splitlines()
    index = 0
    while index + 1 < len(lines):
        raw_code = lines[index].strip()
        if not raw_code:
            index += 1
            continue
        try:
            code = int(raw_code)
        except ValueError:
            return
        yield code, lines[index + 1].rstrip("\r\n")
        index += 2


# Entity names whose group 1 is a text string.
_TEXT_ENTITIES = frozenset({"TEXT", "MTEXT", "ATTRIB", "ATTDEF"})
_SEGMENT_END = frozenset({"SEQEND", "ENDBLK"})


def _dxf_to_lines(content: bytes) -> list[str]:
    """Every text entity in an ASCII DXF, one string per entry."""
    lines: list[str] = []
    entity = ""
    group1 = ""
    group2 = ""

    def flush() -> None:
        nonlocal group1, group2
        value = _mtext_clean(group1) if entity in ("MTEXT",) else group1.strip()
        tag = group2.strip()
        if tag and value:
            # An attribute is a tag/value pair: the titled block's own label.
            lines.append(f"{tag} | {value}")
        elif value:
            lines.append(value)
        group1, group2 = "", ""

    for code, value in _dxf_pairs(content):
        if code == 0:
            flush()
            name = value.strip().upper()
            if name in _TEXT_ENTITIES:
                entity = name
            else:
                entity = ""
            continue
        if entity:
            if code == 1:
                group1 = value
            elif code == 2 and entity in ("ATTRIB", "ATTDEF"):
                group2 = value
    flush()
    return lines


def _labelled(lines: list[str]) -> tuple[list[str], int]:
    """Split lines into canonical ``label | value`` output and a field count."""
    out: list[str] = []
    found = 0
    for line in lines:
        for label, pattern in _LABELED:
            match = pattern.match(line)
            if match:
                value = " ".join(match.group(1).split())
                if value:
                    out.append(f"{label} | {value}")
                    found += 1
                break
    return out, found


# ------------------------------------------------------------- binary scavenge
# A printable run inside a binary object stream. Four characters is the shortest
# that can carry meaning; longer runs are read whole so a value is not cut off
# in the middle.
_PRINTABLE_RUN = re.compile(rb"[\x20-\x7e]{4,}")
# A run has to be at least this long before it is even considered: "BL", "POL"
# and "G.W" are all three characters and would match a label by accident.
_MIN_RUN = 6


def _scavenge_strings(content: bytes, limit: int = 40_000) -> list[str]:
    """Printable ASCII runs in a binary stream, in order."""
    strings: list[str] = []
    for match in _PRINTABLE_RUN.finditer(bytes(content)):
        if len(strings) >= limit:
            break
        text = match.group().decode("ascii", errors="replace").strip()
        if len(text) >= _MIN_RUN:
            strings.append(text)
    return strings


def _dwg_to_lines(content: bytes) -> list[str]:
    """Recover text from a DWG's object stream, evidence-gated.

    DWG is closed and versioned, so the text entities are recovered as strings
    and kept only when the drawing as a whole yields enough labelled fields to
    prove the recovery worked. A short drawing whose text is a bare value with
    no label is therefore reported as unreadable rather than compared against
    noise.
    """
    strings = _scavenge_strings(content)
    if not strings:
        return []
    labelled, found = _labelled(strings)
    if found < MIN_FIELDS:
        return []
    return labelled


def read_cad_text(filename: str, content: bytes) -> Optional[tuple[str, str]]:
    """Read a drawing, returning ``(text, source)`` or None.

    ``source`` records which route produced the text, including
    ``dwg-scavenge``: a value recovered from binary noise is materially less
    trustworthy than one read from an ASCII drawing, and the operator should be
    able to see which it was.
    """
    kind = detect_cad(filename, content)
    if kind is None:
        return None

    try:
        if kind == "dxf-binary":
            return None          # a different container, not textual DXF
        if kind == "dwg":
            lines = _dwg_to_lines(content)
            if not lines:
                return None
            return "\n".join(lines)[:MAX_TEXT_CHARS], "dwg-scavenge"
        if not _is_ascii_dxf(content):
            return None
        lines = _dxf_to_lines(content)
        if not lines:
            return None
        labelled, _ = _labelled(lines)
        # Labelled text first: it is already aligned for the extractor. The
        # remaining strings follow, because a drawing often prints a bare value
        # in a position the label order makes obvious.
        ordered = list(dict.fromkeys(labelled + lines))
        return "\n".join(ordered)[:MAX_TEXT_CHARS], "dxf"
    except Exception as exc:  # noqa: BLE001 - never let a drawing break ingest
        log.info("CAD reader failed for %s (%s): %s", filename, kind, exc)
        return None
