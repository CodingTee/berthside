"""Deterministic comparison engine — the heart of the checker.

Rule from the problem statement:
    Comparison MUST be deterministic.
    SI container_count = 3, BL container_count = 4 → 3 != 4 → MISMATCH
    NOT "LLM: I think this may be a mismatch."

Every field passes through `extractor.normalize` first, then plain equality
(with port special-casing: UN/LOCODE match wins, else normalized name match).
If either side is missing → the field is undecidable, not mismatched.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from app.services.extractor import normalize, ports_match


@dataclass
class ComparisonOutcome:
    status: str                                  # OK | MISMATCH | NEEDS_REVIEW
    has_defect: bool = False
    defect_fields: list[str] = field(default_factory=list)
    undecidable_fields: list[str] = field(default_factory=list)
    review_reason: Optional[str] = None          # set when NEEDS_REVIEW
    field_results: list[dict] = field(default_factory=list)


# Fields where a UN/LOCODE (e.g. CNNTG) is the tiebreaker.
PORT_FIELDS = {"port_of_loading", "port_of_discharge"}


def compare(si_fields: dict[str, Any], bl_fields: dict[str, Any],
            fields: list[str]) -> ComparisonOutcome:
    """Compare extracted SI (reference) against draft BL values."""
    out = ComparisonOutcome(status="OK")

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

        match = _match(f, si_v, bl_v)
        out.field_results.append({
            "field": f, "si_value": si_raw, "bl_value": bl_raw, "match": match,
        })
        if not match:
            out.defect_fields.append(f)

    if out.defect_fields:
        out.status = "MISMATCH"
        out.has_defect = True
    elif out.undecidable_fields:
        out.status = "NEEDS_REVIEW"
        out.review_reason = "missing_value"
    else:
        out.status = "OK"
    return out


def _match(field: str, a: Any, b: Any) -> bool:
    if field in PORT_FIELDS:
        return _match_port(a, b)
    if isinstance(a, float) or isinstance(b, float):
        try:
            return abs(float(a) - float(b)) < 0.001
        except (TypeError, ValueError):
            return False
    return a == b


def _match_port(a: str, b: str) -> bool:
    """Token-set comparison so "NANTONG" matches "NANTONG CHINA CNNTG" —
    one document often prints the UN/LOCODE while the other does not."""
    return ports_match(a, b)
