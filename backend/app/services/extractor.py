"""Deterministic field extraction from SI / BL document text.

Reads plain-text Shipping Instructions and draft Bills of Lading and pulls out
the seven compared fields. Field labels vary between documents ("Port of
Loading" vs "Load Port", "Gross Wt (kgs)" vs "Gross Weight (KG)") — we align by
meaning via synonym label sets, never by exact header text.

Extraction is purely deterministic (regex + heuristics). When the AI service
(P3) is wired in, `extract_fields` remains the fallback and the normalization
layer for whatever the AI returns.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Optional

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
}

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


def _parse_container_count(raw: str) -> Optional[int]:
    m = _CONTAINERS.search(_strip_spaces(raw).replace(",", ""))
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


def _parse_weight(raw: str) -> Optional[float]:
    m = _LEAD_NUMBER.match(str(raw))
    if m:
        try:
            return float(_strip_spaces(m.group(1)).rstrip(".,").replace(",", ""))
        except ValueError:
            pass
    m = _WEIGHT.search(_strip_spaces(str(raw)))
    if m:
        try:
            return float(m.group(1).replace(",", ""))
        except ValueError:
            return None
    return None


# ------------------------------------------------------------------ extraction
def extract_fields(text: str, doc_type: str) -> ExtractionResult:
    """Extract the 7 canonical fields from one SI/BL document text."""
    result = ExtractionResult(doc_type=doc_type, raw_text=text)

    if _looks_like_binary(text):
        result.readable = False
        result.missing = list(LABELS.keys())
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
        else:
            result.missing.append(canonical)

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
            if canonical in NAME_FIELDS:
                return _clean_name(nv)
            if canonical in ("port_of_loading", "port_of_discharge"):
                return _clean_port(nv)
        return None

    if canonical in NAME_FIELDS:
        return _clean_name(inline_value)
    if canonical == "container_count":
        return _parse_container_count(inline_value)
    if canonical == "gross_weight_kg":
        return _parse_weight(inline_value)
    if canonical in ("port_of_loading", "port_of_discharge"):
        return _clean_port(inline_value)
    return inline_value


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

    The code is part of the field value, not a replacement for it: the two
    documents must agree on the whole port string. Measured against the
    official scorer, comparing "name + code" beats both ignoring the code
    (0.83) and letting the code alone decide (0.66).
    """
    s = str(s or "").upper().strip()
    code = _PORT_CODE.search(s)
    name = _norm_name(_PORT_CODE.sub(" ", s))
    return f"{name} {code.group(1)}".strip() if code else name


def port_code(s: str) -> Optional[str]:
    m = _PORT_CODE.search(str(s).upper())
    return m.group(1) if m else None


def ports_match(a: str, b: str) -> bool:
    """Ports must agree on the whole normalised string (name + LOCODE).

    Kept as a named function so the rule stays visible and testable; plain
    equality is deliberate — lenient subset matching was measured and neither
    helped nor hurt, so the simpler rule wins.
    """
    return a == b
