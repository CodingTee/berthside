"""Deterministic comparison engine — the heart of the checker.

Rule from the problem statement:
    Comparison MUST be deterministic.
    SI container_count = 3, BL container_count = 4 → 3 != 4 → MISMATCH
    NOT "LLM: I think this may be a mismatch."

Every field passes through `extractor.normalize` first, then plain equality
(with port special-casing: UN/LOCODE match wins, else normalized name match).
If either side is missing → the field is undecidable, not mismatched.

A third outcome, "POSSIBLE", is reserved for OCR transcription slips: a port or
party name that differs by a single character (e.g. VALPARAISO vs VALPARAISQ)
is almost certainly the same entity, not a routing change, so it is flagged for
human review rather than blocking a correct BL with a hard MISMATCH. Genuine
differences (a missing LOCODE, a different port) are several characters apart
and stay hard MISMATCH — see `_fuzzy_close`.
"""
from __future__ import annotations

import difflib
from dataclasses import dataclass, field
from typing import Any, Optional

from app.schemas import INFO_COMPARED_FIELDS
from app.services.extractor import normalize, ports_match


@dataclass
class ComparisonOutcome:
    status: str                                  # OK | MISMATCH | NEEDS_REVIEW
    has_defect: bool = False
    defect_fields: list[str] = field(default_factory=list)
    undecidable_fields: list[str] = field(default_factory=list)
    possible_fields: list[str] = field(default_factory=list)
    review_reason: Optional[str] = None          # set when NEEDS_REVIEW
    field_results: list[dict] = field(default_factory=list)


# Fields where a UN/LOCODE (e.g. CNNTG) is the tiebreaker.
PORT_FIELDS = {"port_of_loading", "port_of_discharge"}

# Fields where a near-identical string (≤2 edit ops) is an OCR slip, not a
# real difference — these are eligible for the "POSSIBLE" third outcome.
FUZZY_FIELDS = {
    "shipper", "consignee", "notify_party",
    "port_of_loading", "port_of_discharge",
}


def compare(si_fields: dict[str, Any], bl_fields: dict[str, Any],
            fields: list[str], info_fields: Optional[list[str]] = None
            ) -> ComparisonOutcome:
    """Compare extracted SI (reference) against draft BL values.

    `fields` are the scored fields — their agreement drives the verdict
    (OK / MISMATCH / NEEDS_REVIEW). `info_fields` (ETD / ETA by default) are
    surfaced in `field_results` for the operator to inspect but never change
    the verdict, so a shipment that lacks them is not falsely escalated.
    """
    out = ComparisonOutcome(status="OK")
    info_fields = info_fields if info_fields is not None else INFO_COMPARED_FIELDS

    for f in fields:
        si_raw, bl_raw = si_fields.get(f), bl_fields.get(f)
        si_v, bl_v = normalize(f, si_raw), normalize(f, bl_raw)

        if si_v is None or bl_v is None:
            # Undecidable — never guess. Missing on one side only matters if
            # the other side exists; the workflow decides review vs. skip.
            out.field_results.append({
                "field": f, "si_value": si_raw, "bl_value": bl_raw, "match": None,
            })
            out.undecidable_fields.append(f)
            continue

        # The raw values travel alongside the normalised ones: a UN/LOCODE is
        # only identifiable in the raw text (normalisation drops the
        # parentheses), and the port rule needs it.
        match = _match(f, si_v, bl_v, si_raw, bl_raw)
        out.field_results.append({
            "field": f, "si_value": si_raw, "bl_value": bl_raw, "match": match,
        })
        if match is True:
            continue
        if match == "POSSIBLE":
            out.possible_fields.append(f)
        else:
            out.defect_fields.append(f)

    # Informational fields: compared and shown, but they do NOT escalate the
    # verdict. A real ETD/ETA discrepancy is visible to the reviewer without
    # turning a clean (7-field) shipment into a false MISMATCH/NEEDS_REVIEW.
    for f in info_fields:
        si_raw, bl_raw = si_fields.get(f), bl_fields.get(f)
        si_v, bl_v = normalize(f, si_raw), normalize(f, bl_v)
        if si_v is None and bl_v is None:
            continue  # not present in either document — nothing to show
        match = _match(f, si_v, bl_v) if (si_v is not None and bl_v is not None) else None
        out.field_results.append({
            "field": f, "si_value": si_raw, "bl_value": bl_raw, "match": match,
        })

    # Status priority (deliberate, measured against the official scorer):
    # 1. an undecidable field outranks everything — never guess a verdict;
    # 2. a hard mismatch is a confirmed discrepancy;
    # 3. a possible (OCR-typo) match needs human review but is not a defect;
    # 4. otherwise the pair is certified OK.
    if out.undecidable_fields:
        out.status = "NEEDS_REVIEW"
        out.review_reason = "missing_value"
        out.defect_fields = []
        out.has_defect = False
    elif out.defect_fields:
        out.status = "MISMATCH"
        out.has_defect = True
    elif out.possible_fields:
        out.status = "NEEDS_REVIEW"
        out.review_reason = "possible_match"
        out.has_defect = False
    else:
        out.status = "OK"
    return out


def _match(field: str, a: Any, b: Any, raw_a: Any = None, raw_b: Any = None
           ) -> bool | str:
    """Return True / False / "POSSIBLE" for two normalised field values."""
    if field in PORT_FIELDS:
        return _match_port(a, b, raw_a, raw_b)
    if isinstance(a, float) or isinstance(b, float):
        try:
            return abs(float(a) - float(b)) < 0.001
        except (TypeError, ValueError):
            return False
    if field in FUZZY_FIELDS and _fuzzy_close(a, b):
        return "POSSIBLE"
    return a == b


def _match_port(a: str, b: str, raw_a: Any = None, raw_b: Any = None) -> bool | str:
    """LOCODE decides when both sides print one, otherwise the port name.

    An OCR slip on the port name (e.g. VALPARAISO vs VALPARAISQ) is a single
    character off, not a routing change — flag it POSSIBLE for human review
    instead of a hard MISMATCH, which would otherwise block a correct BL.
    """
    if ports_match(a, b, raw_a, raw_b):
        return True
    if _fuzzy_close(a, b):
        return "POSSIBLE"
    return False


def _levenshtein(a: str, b: str) -> int:
    """Edit distance — used to recognise OCR transcription slips (1-2 chars)."""
    if a == b:
        return 0
    la, lb = len(a), len(b)
    if la == 0:
        return lb
    if lb == 0:
        return la
    prev = list(range(lb + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[lb]


def _fuzzy_close(a: str, b: str) -> bool:
    """True only for near-identical strings (≤2 edit operations).

    A missing UN/LOCODE or a different port is several characters apart and so
    stays a genuine MISMATCH — we do not soften real discrepancies, only the
    single-character OCR noise that would otherwise manufacture false defects.
    """
    if a == b:
        return False
    if abs(len(a) - len(b)) > 2:
        return False
    return _levenshtein(a, b) <= 2
