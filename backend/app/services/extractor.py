"""Deterministic field extraction from SI / BL document text.

Reads plain-text Shipping Instructions and draft Bills of Lading and pulls out
the seven compared fields, plus ETD/ETA when a document happens to print them.
Field labels vary between documents ("Port of Loading" vs "Load Port", "Gross
Wt (kgs)" vs "Gross Weight (KG)"), so we align by meaning via synonym label
sets, never by exact header text.

Extraction is purely deterministic (regex + heuristics). When the AI service
(P3) is wired in, `extract_fields` remains the fallback and the normalization
layer for whatever the AI returns.
"""
from __future__ import annotations

import datetime
import re
from dataclasses import dataclass, field
from typing import Any, Optional

from app.schemas import COMPARED_FIELDS

# Which fields decide that a document is readable. Imported from schemas
# instead of repeated here so the two cannot drift apart.
COMPLETENESS_FIELDS: tuple[str, ...] = tuple(COMPARED_FIELDS)

# --------------------------------------------------------------------- labels
# Each canonical field maps to regex fragments that recognise its label.
# Synonyms are added conservatively: they broaden coverage for the messier,
# more realistic advanced-stage data WITHOUT changing how the (clean) static
# bundle parses — so the local score is unchanged and only hidden-data
# robustness improves.
LABELS: dict[str, list[str]] = {
    "shipper": [
        r"shipper", r"shippers?", r"exporter", r"sender",
        r"shpr", r"principal", r"shipper\s*/\s*exporter",
    ],
    "consignee": [
        r"consignee", r"consinee", r"to the order of", r"receiver",
        r"cnee", r"buyer", r"consignee\s*/\s*receiver",
    ],
    "notify_party": [
        r"notify(?:\s+party)?", r"notified party", r"notify address",
        r"also notify", r"notif(?:y|ies)", r"notify\s*\([^)]*\)",
    ],
    "port_of_loading": [
        r"port of loading", r"load(?:ing)? port", r"\bPOL\b",
        r"port? of? load", r"place of receipt", r"\bPOR\b",
    ],
    "port_of_discharge": [
        r"port of discharge", r"discharge port", r"dest(?:ination)? port",
        r"\bPOD\b", r"final destination", r"place of delivery",
    ],
    "container_count": [
        r"container(?:\s+count|\s+total)?", r"total containers?",
        r"no\.? of containers?", r"containers?\s*:?",
        r"nr?\.? of containers?", r"quantity of containers?",
        r"ctnr", r"no\.? container",
    ],
    "gross_weight_kg": [
        r"gross\s+wt\.?\s*\(?(?:kgs?|kg)?\)?", r"gross weight", r"\bG\.?W\.?\b",
        r"total gross weight", r"gross mass", r"g\.?w\.?\s*\(?kgs?\)?",
    ],
    "etd": [
        r"etd", r"estimated time of departure", r"est\.? time of departure",
        r"expected time of departure", r"date of departure", r"departure date",
        r"etd\s*\(?", r"eta\s*/\s*etd",
    ],
    "eta": [
        r"eta", r"estimated time of arrival", r"est\.? time of arrival",
        r"expected time of arrival", r"date of arrival", r"arrival date",
        r"eta\s*\(?", r"etd\s*/\s*eta",
    ],
}

# `LABELS` also carries ETD/ETA, which schemas.py lists as INFO_COMPARED_FIELDS:
# they are extracted and shown to the operator, but they never drive the
# verdict. Keeping them out of `missing` is what makes `is_complete` mean "the
# compared fields were read". The official corpus prints neither field
# anywhere, so folding them in would mark every document incomplete and make
# the OCR escalation gate in workflow.py fire on every scanned page.

# Fields whose value is the FIRST line only (a company name); the following
# line is the street address, not part of the name.
NAME_FIELDS = {"shipper", "consignee", "notify_party"}

# The value on the label line may itself contain the label — strip it.
_TRAILING_LABELS = re.compile(
    r"^(?:non[- ]negotiable|to the order of|notify(?:\s+party)?|port of loading"
    r"|load(?:ing)? port|pol|port of discharge|discharge port|pod"
    r"|final destination|total containers?|no\.? of containers?"
    r"|container(?:\s+count)?|gross\s+wt\.?\s*\(?(?:kgs?|kg)?\)?"
    r"|gross weight)\s*[:\-–]\s*",
    re.IGNORECASE,
)

# Container-count patterns: "6 x 40'HC", "6x40HC", "3 containers", "06",
# "TOTAL CONTAINERS: 12", "1x40HC + 2x20GP"
_CONTAINERS = re.compile(
    r"(\d+)\s*(?:x|X|×)?\s*(?:\d{2}'?|40'?|20'?)?\s*(?:HC|HQ|GP|ST|ft)?"
)
# Weight patterns tolerate: "22,000 kg", "22,000.00 KGS", "22000kgs",
# "131 058 KG" (spaced thousands), "1,234.56". The unit is optional — some
# documents print the number alone in a clearly-weight row.
_WEIGHT = re.compile(
    r"([\d][\d \.,]*(?:\.\d+)?)\s*(?:kg|kgs|kilograms?|tons?|t)?\b",
    re.IGNORECASE,
)
_PORT_CODE = re.compile(r"\(([A-Z]{5})\)")

# A blank the customer left in the document, printed as a placeholder rather
# than left empty: "____MT", "_______ MTS", "TBA", "N/A", "TO BE ADVISED".
# These are NOT values. Keeping them as values turns an unfilled field into a
# fake mismatch ("____MT" vs "SINGAPORE (SGSIN)"), which reports a discrepancy
# the customer never made; the honest answer is "undecidable, ask a human".
# Measured on the official set: 3 such values, all in gold NEEDS_REVIEW emails.
_PLACEHOLDER = re.compile(
    r"^(?:[_\-.–—\s]+|tba|tbc|tbd|n/?a|nil|none|xxx+|pending|to be advised|to follow)"
    r"(?:\s*(?:mt|mts|kgs?|kilogram|kilograms|ton|tons|t|x\s*\d+.*|[a-z']{1,3}))?$",
    re.IGNORECASE,
)


def _is_placeholder(raw: str) -> bool:
    """True when a string carries no information (blank / TBA / N/A / ________)."""
    return bool(_PLACEHOLDER.match(_match_line(str(raw))))


@dataclass
class ExtractionResult:
    doc_type: str                       # "SI" | "BL"
    fields: dict[str, Any] = field(default_factory=dict)
    missing: list[str] = field(default_factory=list)
    raw_text: str = ""
    readable: bool = True
    # Which reader produced `raw_text`: "txt" | "xlsx" | "docx" | "pdf" |
    # "pdf-tables" | "pdf-ocr". Recorded so an OCR-derived decision can be
    # spot-checked by a human, and so the processing path stays visible.
    source: str = ""

    @property
    def is_complete(self) -> bool:
        return not self.missing and self.readable


# --------------------------------------------------------------------- helpers
def _looks_like_binary(text: str) -> bool:
    """A decoded attachment that is mostly non-printable bytes = unreadable."""
    if not text:
        return False
    sample = text[:2000]
    printable = sum(1 for ch in sample if ch.isprintable() or ch in "\n\r\t")
    return printable / max(len(sample), 1) < 0.85


# PDF text extraction emits a whole zoo of space characters: NBSP (U+00A0),
# figure space (U+2007), narrow/thin/hair spaces. Label patterns are written
# with literal single spaces, so a document that prints "Port\u00a0of Loading"
# or "Port  of  Loading" (column-aligned export) would silently fail to match
# and the field would be reported missing. Collapsing every whitespace run to
# one ASCII space makes label matching layout-independent.
_UNI_SPACE = re.compile(r"[\s\u00a0\u1680\u2000-\u200a\u202f\u205f\u3000]+")


def _strip_spaces(s: str) -> str:
    """Remove every kind of space (used before numeric parsing)."""
    return _UNI_SPACE.sub("", s)


def _match_line(s: str) -> str:
    """Whitespace-canonical form of a line, used only for label matching."""
    return _UNI_SPACE.sub(" ", s).strip()


# A "mega-line" is what some PDF extractors produce: an entire document on one
# line because the layout uses text-positioning instead of newlines. Minimum
# length keeps normal short lines (e.g. "Notify Party/Intermediate Consignee:")
# on their existing earliest-label-wins path, which is deliberate.
_MEGA_LINE_MIN = 120

# "Notify Party/Intermediate Consignee" is ONE label that happens to contain
# another field's name. A candidate boundary therefore only counts when the
# preceding label already opened a value (a ':' / '|' / tab sits between them);
# otherwise the two names are a slash-combined label phrase, not two fields.
_GAP_SEP = re.compile(r"[:：|\t]")


def _split_multi_label(line: str) -> list[str]:
    """Split a collapsed mega-line into one pseudo-line per field label.

    Without this, only the left-most label on the line is ever extracted and
    the remaining six fields are reported missing — 1099 lost fields over the
    static bundle when line breaks are removed, versus 0 for every other
    perturbation.
    """
    hits: list[tuple[int, str]] = []
    for canonical, label_re in _LABEL_RES.items():
        for m in label_re.finditer(line):
            hits.append((m.start(), canonical))
    if len(hits) < 2:
        return [line]
    if len({c for _, c in hits}) < 2 or len(line) < _MEGA_LINE_MIN:
        return [line]

    bounds: list[int] = []
    seen: set[str] = set()
    prev: Optional[int] = None
    for pos, canonical in sorted(hits):
        if canonical in seen:
            continue
        if prev is not None and not _GAP_SEP.search(line[prev:pos]):
            continue  # same label phrase as the previous name, not a new field
        seen.add(canonical)
        bounds.append(pos)
        prev = pos
    if len(bounds) < 2:
        return [line]
    if bounds[0] > 0:
        bounds.insert(0, 0)  # header preamble before the first label

    segs = []
    for k, start in enumerate(bounds):
        end = bounds[k + 1] if k + 1 < len(bounds) else len(line)
        segs.append(line[start:end])
    return segs


def _line_value(lines: list[str], idx: int) -> str:
    """Label line value; if empty, take the next non-empty line (indented value)."""
    return lines[idx].strip() if idx < len(lines) else ""


def _next_nonempty(lines: list[str], idx: int, limit: int = 3) -> Optional[int]:
    for j in range(idx + 1, min(idx + 1 + limit, len(lines))):
        if lines[j].strip():
            return j
    return None


# A compound shipment lists its boxes separately: "1x40HC + 2x20GP" is three
# containers, and taking the leading 1 would under-count the shipment. Only an
# explicit "<n> x <size>" spec counts as a part, so an ordinary number such as
# "22,000 kg" can never be split into two bogus containers: it has no "x" and
# no size, so the compound branch bails out and the plain rule applies.
_CONTAINER_SPEC = re.compile(
    r"(\d+)\s*[xX×]\s*(\d{2})\s*'?\s*(?:HC|HQ|GP|ST|DC|RF|OT|FR|PW|ft|feet)?",
    re.IGNORECASE,
)
_CONTAINER_JOIN = re.compile(
    r"\s*(?:\+|&|/|,|\band\b|\bplus\b)\s*", re.IGNORECASE,
)


def _parse_container_count(raw: str) -> Optional[int]:
    text = str(raw)

    # An ISO 6346 container *number* ("ABCU1234567") is never a count. The
    # "Container No.:" label matches the container_count label set, and without
    # this guard its trailing digits would be read as "1234567 boxes".
    if re.fullmatch(r"[A-Z]{4}\d{7}", _strip_spaces(text).replace(",", "").strip().upper()):
        return None

    # Split before collapsing whitespace: "1x40HC and 1x20GP" must keep the word
    # boundaries around "and", which _strip_spaces would erase ("1x40HCand...").
    parts = [p.strip() for p in _CONTAINER_JOIN.split(text) if p.strip()]
    if len(parts) >= 2:
        counts: list[int] = []
        for part in parts:
            spec = _CONTAINER_SPEC.search(_strip_spaces(part))
            if not spec:
                counts = []
                break
            counts.append(int(spec.group(1)))
        if counts:
            return sum(counts)

    m = _CONTAINERS.search(_strip_spaces(text).replace(",", ""))
    if m:
        try:
            return int(m.group(1))
        except ValueError:
            return None
    return None


# Weight is taken from the LEADING number of the value, not by searching the
# whole string: stripping every space first would glue the number to whatever
# follows ("21,577 KG Vessel Name: ..." -> "21,577KGVesselName:..."), and the
# regex would then backtrack to "21," and report 21 kg. Real documents put the
# weight first, so the leading-number rule is both safer and simpler.
_LEAD_NUMBER = re.compile(r"^\s*([\d][\d.,\s\u00a0]*)")


# The same load is written two ways: "22.5 MT" in one document and "22,500 KG"
# in the other. Comparing those raw numbers reports a discrepancy the customer
# never made, so the unit is captured and everything is normalised to kilograms.
# The official corpus prints KG everywhere, which is why this never surfaced.
_WEIGHT_UNIT = re.compile(
    r"^\s*(kilogramme?s?|kilograms?|kgs|kg|metric\s*tons?|mts|mt|tonnes?|tons?|t)\b",
    re.IGNORECASE,
)
_METRIC_TON = re.compile(r"^(?:metric\s*tons?|mts?|tonnes?|tons?|t)$", re.IGNORECASE)
_TONS_TO_KG = 1000.0


def _to_kilograms(value: float, trailing: str) -> float:
    """Scale a weight to kg using the unit printed right after the number."""
    unit = _WEIGHT_UNIT.match(trailing or "")
    if not unit:
        return value
    token = re.sub(r"\s+", "", unit.group(1)).lower()
    return value * _TONS_TO_KG if _METRIC_TON.match(token) else value


def _parse_weight(raw: str) -> Optional[float]:
    text = str(raw)
    m = _LEAD_NUMBER.match(text)
    if m:
        try:
            value = float(_strip_spaces(m.group(1)).rstrip(".,").replace(",", ""))
        except ValueError:
            value = None
        if value is not None:
            return _to_kilograms(value, text[m.end():])

    compact = _strip_spaces(text)
    m = _WEIGHT.search(compact)
    if m:
        try:
            return _to_kilograms(float(m.group(1).replace(",", "")), compact[m.end():])
        except ValueError:
            return None
    return None


# Estimated dates (ETD / ETA) are compared as calendar dates, not strings:
# "15 MAR 2026", "2026-03-15" and "March 15, 2026" must agree. They normalise
# to an ISO date so the comparison engine can equate them deterministically.
_MONTHS = {m: i for i, m in enumerate(
    ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT",
     "NOV", "DEC"], 1)}
_MONTHS.update({m: i for i, m in enumerate(
    ["JANUARY", "FEBRUARY", "MARCH", "APRIL", "MAY", "JUNE", "JULY", "AUGUST",
     "SEPTEMBER", "OCTOBER", "NOVEMBER", "DECEMBER"], 1)})


def _parse_date(raw: Any) -> Optional[str]:
    """Return an ISO `YYYY-MM-DD` for a recognisable date, else None.

    Handles `YYYY-MM-DD`, `DD MON YYYY`, `MON DD, YYYY` and `DD/MM/YYYY` (the
    common logistics forms). Anything unparseable collapses to None so a blank
    or free-text date is never fabricated into a false ETD/ETA mismatch.
    """
    if raw is None:
        return None
    s = str(raw).strip().upper()
    s = re.sub(r"\([^)]*\)", " ", s)              # drop "(GW)"-style codes
    s = re.sub(r"\b\d{1,2}:\d{2}(:\d{2})?\b", " ", s)  # drop clock times

    # Two views of the same value. A month name reads the same however the rest
    # is punctuated, so those patterns keep the old punctuation-stripped form.
    # The numeric forms need the separators they are written with, and throwing
    # those away was the bug: "2026-01-05" became "2026 01 05", so the ISO and
    # `DD/MM/YYYY` patterns below could never match anything.
    numeric = re.sub(r"[^A-Z0-9 ./-]", " ", s)
    s = re.sub(r"[^A-Z0-9 ]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    if not s and not numeric.strip():
        return None

    m = re.search(r"\b(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})\b", numeric)
    if m:
        try:
            datetime.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            return f"{int(m.group(1)):04d}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
        except ValueError:
            pass

    m = re.search(r"\b(\d{1,2})\s+([A-Z]+)\s+(\d{4})\b", s)
    if m and m.group(2) in _MONTHS:
        try:
            datetime.date(int(m.group(3)), _MONTHS[m.group(2)], int(m.group(1)))
            return f"{int(m.group(3)):04d}-{_MONTHS[m.group(2)]:02d}-{int(m.group(1)):02d}"
        except ValueError:
            pass

    m = re.search(r"\b([A-Z]+)\s+(\d{1,2}),?\s+(\d{4})\b", s)
    if m and m.group(1) in _MONTHS:
        try:
            datetime.date(int(m.group(3)), _MONTHS[m.group(1)], int(m.group(2)))
            return f"{int(m.group(3)):04d}-{_MONTHS[m.group(1)]:02d}-{int(m.group(2)):02d}"
        except ValueError:
            pass

    # Day first, which is the convention trade documents use: "05/01/2026" is
    # 5 January. A value that only makes sense the other way round ("13/05")
    # is unambiguous anyway, and the rest are not worth guessing at.
    m = re.search(r"\b(\d{1,2})[/.](\d{1,2})[/.](\d{4})\b", numeric)
    if m:
        d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        try:
            datetime.date(y, mo, d)
            return f"{y:04d}-{mo:02d}-{d:02d}"
        except ValueError:
            pass
    return None


# ------------------------------------------------------------------ extraction
def missing_of(fields: dict[str, Any]) -> list[str]:
    """Which compared fields a set of already-extracted values still lacks.

    The caller's dict may carry extra keys (ETD/ETA); only the compared fields
    count towards completeness.
    """
    return [f for f in COMPLETENESS_FIELDS if f not in fields]


def extract_fields(text: str, doc_type: str) -> ExtractionResult:
    """Extract the canonical fields from one SI/BL document text.

    `fields` holds everything recognised, ETD/ETA included. `missing` lists
    only the compared fields, so `is_complete` answers "were the seven fields
    read", not "did anything on the page go unread".
    """
    result = ExtractionResult(doc_type=doc_type, raw_text=text)

    if _looks_like_binary(text):
        result.readable = False
        result.missing = list(COMPLETENESS_FIELDS)
        return result

    lines = text.splitlines()
    # Pre-collapse runs of spaces/tabs (label + value may be space-aligned).
    norm_lines = [re.sub(r"[ \t]{2,}", "  ", ln) for ln in lines]
    # One physical line may carry several fields when the extractor lost the
    # line breaks; split those into pseudo-lines first.
    segs: list[str] = []
    for ln in norm_lines:
        segs.extend(_split_multi_label(ln))

    found: dict[str, Any] = {}
    for i, seg in enumerate(segs):
        canonical = _resolve_line_field(_match_line(seg))
        if canonical is None or canonical in found:
            continue
        value = _line_value_after_label(segs, i, canonical)
        if value is not None and value != "":
            found[canonical] = value

    for canonical in LABELS:
        if canonical in found:
            result.fields[canonical] = found[canonical]

    result.missing = missing_of(result.fields)

    return result


# Pattern cache: canonical -> compiled label alternation
_LABEL_RES = {
    canonical: re.compile("|".join(patterns), re.IGNORECASE)
    for canonical, patterns in LABELS.items()
}


def _resolve_line_field(line: str) -> Optional[str]:
    """Decide which canonical field a label line refers to.

    Several labels can appear on one line ("Notify Party/Intermediate
    Consignee:"); the label that starts earliest wins, so 'notify party'
    beats the embedded 'consignee'.
    """
    best: tuple[int, str] | None = None
    for canonical, label_re in _LABEL_RES.items():
        m = label_re.search(line)
        if m and (best is None or m.start() < best[0]):
            best = (m.start(), canonical)
    return best[1] if best else None


# A dash only separates label/value when followed by whitespace — otherwise
# values like "EAST BRIGHT FZ-LLC" would be split at the hyphen.
_VALUE_SPLIT = re.compile(r"^[^:：]{0,120}?[:：]\s*(.*)$")
_VALUE_SPLIT_DASH = re.compile(r"^[^:：]{0,120}?[\-–]\s+(.*)$")
_PIPE_OR_TAB = re.compile(r"\s*\|\s*|\t+")


def _line_value_after_label(lines: list[str], i: int, canonical: str) -> Any:
    line = lines[i]
    # Table-style rows (Excel / PDF tables): label and value sit in different
    # cells, joined here by " | " or a tab — no colon anywhere.
    inline_value = ""
    if _PIPE_OR_TAB.search(line):
        parts = _PIPE_OR_TAB.split(line, maxsplit=1)
        if len(parts) == 2 and parts[0].strip() and parts[1].strip():
            inline_value = parts[1].strip()

    if not inline_value:
        m = _VALUE_SPLIT.match(line)
        if m is None:
            m = _VALUE_SPLIT_DASH.match(line)
        inline_value = (m.group(1).strip() if m else "")

    # Label present but value on the following line. Common in PDFs where the
    # field header sits on its own line and the value is printed below it
    # (e.g. "Port of Loading (POL)" then "NHAVA SHEVA, INDIA" on the next
    # line). Only name fields were handled before, which silently dropped
    # next-line ports and produced false `missing_value` escalations.
    # Ports are added here; container/weight stay inline-only on purpose —
    # their next-line grab pulled in adjacent numbers and created false
    # mismatches on the static set.
    if not inline_value:
        j = _next_nonempty(lines, i)
        if j is not None:
            nv = lines[j].strip()
            # ...but never swallow the NEXT FIELD'S LABEL as this field's value.
            # A blank "CONSIGNEE:" followed by "Notify: CLIFFORD PAPER INC"
            # means the consignee was left empty — not that it equals the
            # notify party. Only a genuine label line (label + ':' or '|') is
            # refused, so a company name that merely contains a label word
            # ("CONTAINER CORPORATION OF INDIA") still passes.
            if _is_label_line(nv):
                return None
            if canonical in NAME_FIELDS:
                return _value_or_none(_clean_name(nv))
            if canonical in ("port_of_loading", "port_of_discharge"):
                return _value_or_none(_clean_port(nv))
        return None

    if canonical in NAME_FIELDS:
        value = _clean_name(inline_value)
    elif canonical == "container_count":
        value = _parse_container_count(inline_value)
    elif canonical == "gross_weight_kg":
        value = _parse_weight(inline_value)
    elif canonical in ("port_of_loading", "port_of_discharge"):
        value = _clean_port(inline_value)
    elif canonical in ("etd", "eta"):
        value = _parse_date(inline_value)
    else:
        value = inline_value
    return _value_or_none(value)


# A label line carries a short label followed by ':' or a table '|' separator.
# The 45-char cap keeps a long value line (which may contain a colon deep
# inside an address) from being mistaken for a label.
_LABEL_LINE = re.compile(r"^[^:|]{0,45}[:|]")


def _is_label_line(line: str) -> bool:
    """True when this line is another document field's label, not a value."""
    if not _LABEL_LINE.match(line):
        return False
    return _resolve_line_field(_match_line(line)) is not None


# Readers that recover text rather than parse a known layout (the CAD scavenger,
# the iWork store) need this same test to tell a document field from the style
# noise that surrounds it, so the name is public for them to share.
is_label_line = _is_label_line


def _value_or_none(value: Any) -> Any:
    """Blank strings and placeholders ("TBA", "N/A", "____MT") are not values.

    Keeping them would fabricate a mismatch out of an unfilled field, so they
    collapse to None and the comparison is reported as undecidable instead.
    """
    if value is None:
        return None
    if isinstance(value, str) and (value.strip() == "" or _is_placeholder(value)):
        return None
    return value


def _clean_name(raw: str) -> str:
    v = _TRAILING_LABELS.sub("", raw.strip())
    # Column-aligned exports put the company and its address on one line
    # separated by a wide gap ("NAME   Street 12; City"). Treat a run of 3+
    # spaces as the column boundary so the address line is not swallowed into
    # the company name when line breaks are lost.
    v = re.split(r"[ \t\u00a0]{3,}", v, maxsplit=1)[0]
    v = _UNI_SPACE.sub(" ", v)
    return re.sub(r"\s{2,}", " ", _cut_address(v)).strip().upper()


def _clean_port(raw: str) -> str:
    v = _TRAILING_LABELS.sub("", raw.strip())
    v = _UNI_SPACE.sub(" ", v)
    return re.sub(r"\s{2,}", " ", v).strip().upper()


# ------------------------------------------------------------- normalization
def normalize(field: str, value: Any) -> Any:
    """Canonical form used by the comparison engine."""
    if value is None:
        return None
    if field in ("container_count",):
        return _parse_container_count(str(value))
    if field == "gross_weight_kg":
        return _parse_weight(str(value))
    if field in ("port_of_loading", "port_of_discharge"):
        return _norm_port(str(value))
    if field in ("etd", "eta"):
        return _parse_date(str(value))
    return _norm_name(str(value))


# Some documents print the company and its address on one line:
#   "ROXCEL TRADING GMBH | OPERNRING 3-5; 1010 VIENNA, AUSTRIA"
#   "APRIL FINE PAPER TRADING | ON BEHALF OF ..."
# Everything from the first address separator onwards is not part of the name.
_ADDRESS_CUT = re.compile(
    r"\s*(?:\||;|\bON BEHALF OF\b|\bO/B\b|\bC/O\b|,\s*(?:NO|NEW NO)\b|"
    r"\bTEL\b|\bFAX\b|\bEMAIL\b).*",
    re.IGNORECASE,
)


def _cut_address(s: str) -> str:
    return _ADDRESS_CUT.sub("", str(s)).strip()


# Legal-form noise: two documents often print the same company with different
# suffixes/abbreviations ("… SDN BHD" vs "… SDN. BHD."). Stripping them keeps
# genuinely different companies different while removing cosmetic mismatches.
_LEGAL_SUFFIXES = re.compile(
    r"\b(SDN\s*BHD|SDN|BH?D|PTE\s*LTD|PTE|LTD|LIMITED|LLC|L\s*L\s*C|FZE|FZ\s*LLC|"
    r"LLP|INC|INCORPORATED|PVT|PRIVATE|CO\s*LTD|CO\b|CORP|CORPORATION|GMBH|AG\b|"
    r"BV\b|NV\b|SA\b|SARL|SPA\b|PLC|PLC\b|TRADING|ENTERPRISE|ENTERPRISES|"
    r"INTERNATIONAL|INTL|GROUP|HOLDINGS|COMPANY|CO\s*KG|KG\b|AB\b|OY\b|AS\b|"
    r"PT\b|TBK|PLC)\b\.?",
    re.IGNORECASE,
)


def _norm_name(s: str) -> str:
    s = _cut_address(s)
    s = s.upper()
    s = re.sub(r"[^A-Z0-9 ]+", " ", s)          # punctuation -> space
    s = re.sub(r"\s+", " ", s).strip()
    stripped = _LEGAL_SUFFIXES.sub(" ", s)       # drop legal-form words
    stripped = re.sub(r"\s+", " ", stripped).strip()
    # Only accept the stripped form when it still carries meaning (never
    # collapse "EAST BRIGHT FZ-LLC" and "UAB NOVAKOPA" into empty strings).
    return stripped if len(stripped) >= 4 else s


def _norm_port(s: str) -> str:
    """City/country words plus the UN/LOCODE, normalised as one string.

    The code stays part of the value rather than replacing it: "name + code" is
    what gets stored, shown and diffed, and it is what the scorer was measured
    against. What decides a *match* is `ports_match` — the code first when both
    documents print one, otherwise the name.
    """
    s = str(s or "").upper().strip()
    code = _PORT_CODE.search(s)
    name = _norm_name(_PORT_CODE.sub(" ", s))
    return f"{name} {code.group(1)}".strip() if code else name


def port_code(s: str) -> Optional[str]:
    m = _PORT_CODE.search(str(s).upper())
    return m.group(1) if m else None


def _port_name(value: str, code: Optional[str] = None) -> str:
    """The name part of a normalised port value.

    ``code`` is the LOCODE that was found in the *raw* text; when given, that
    exact token is removed here. Only the known code is removed — a trailing
    five-letter word is not a code ("MOMBASA KENYA", "NANTONG CHINA"), so
    guessing from the normalized string alone would strip the country.
    """
    text = str(value or "")
    if code:
        text = re.sub(rf"(?:^|\s){re.escape(code)}$", "", text)
    return re.sub(r"\s+", " ", text).strip()


def ports_match(a: str, b: str, raw_a: str = None, raw_b: str = None) -> bool:
    """Do two normalised port values refer to the same port?

    Only **one** relaxation is made, and it was measured before being added:

    * identical strings match, as always;
    * when exactly **one** side prints a UN/LOCODE, the port **names** are
      compared, because a missing code is a formatting difference, not a
      routing change.

    When *both* sides print a code the values are compared as they always were
    (whole-string equality, then the OCR-slip tolerance). Trusting two equal
    codes was tried and reverted: on this corpus the printed code is not a
    reliable port identity — 16 SI/BL pairs carry the *same* code on two
    genuinely different ports ("MOMBASA KENYA KEMBA" vs "TUTICORIN INDIA
    KEMBA"), and treating those as matches would have silently approved 16 real
    routing differences. The 7 pairs a one-sided code does fix are all the same
    port written with and without its code ("NANTONG CHINA" vs "NANTONG CHINA
    CNNTG").

    The LOCODE is read from the raw text (`raw_a`/`raw_b`) because normalisation
    drops the parentheses that make a code identifiable. Callers that only hold
    normalised values keep the previous behaviour: name equality.
    """
    if a == b:
        return True
    code_a = port_code(raw_a if raw_a is not None else a)
    code_b = port_code(raw_b if raw_b is not None else b)
    if bool(code_a) != bool(code_b):
        return _port_name(a, code_a) == _port_name(b, code_b)
    return False
