"""Normalisation + field-by-field comparison."""
from __future__ import annotations

import re
from dataclasses import dataclass, field as dc_field

BLANK_TOKENS = {"", "???", "_______", "TBA", "TBC", "N/A", "____MT"}


@dataclass
class FieldComparison:
    field: str
    si_raw: str | None = None
    bl_raw: str | None = None
    si_value: str | None = None
    bl_value: str | None = None
    match: bool | None = None       # None => at least one side missing
    missing: bool = False


def normalize_value(field: str, raw: str | None) -> str | None:
    """Canonical comparable value; None when blank/missing."""
    if raw is None:
        return None
    v = re.sub(r"\s+", " ", raw).strip().rstrip(".,;")
    if v.upper() in {b.upper() for b in BLANK_TOKENS}:
        return None
    if field == "container_count":
        m = re.match(r"(\d+)\s*x\s*", v)
        return m.group(1) if m else v
    if field == "gross_weight_kg":
        m = re.search(r"([\d,]+)", v)
        return m.group(1).replace(",", "") if m else v
    return v.upper()


def compare_fields(si: dict[str, str], bl: dict[str, str],
                   fields: list[str]) -> list[FieldComparison]:
    out = []
    for f in fields:
        c = FieldComparison(field=f)
        c.si_raw, c.bl_raw = si.get(f), bl.get(f)
        c.si_value = normalize_value(f, c.si_raw)
        c.bl_value = normalize_value(f, c.bl_raw)
        c.missing = c.si_value is None or c.bl_value is None
        if c.missing:
            c.match = None
        else:
            c.match = c.si_value == c.bl_value
        out.append(c)
    return out


def defect_fields(comparisons: list[FieldComparison]) -> list[str]:
    return sorted(c.field for c in comparisons if c.match is False)
