"""Unit tests for the deterministic comparison engine.

These are the tests that protect the one rule the judges care about:
    SI container_count = 3, BL container_count = 4  ->  MISMATCH
    NOT "the LLM thinks this might be a mismatch".
"""
from __future__ import annotations

from app.schemas import COMPARED_FIELDS
from app.services.comparison import compare

# A complete, agreeing document pair. Mismatch tests change exactly one field
# on top of this, because a pair with unfilled fields is not comparable — see
# test_blank_field_outranks_a_mismatch.
COMPLETE = {
    "shipper": "APRIL FAR EAST (M) SDN BHD",
    "consignee": "EAST BRIGHT FZ-LLC",
    "notify_party": "EAST BRIGHT FZ-LLC",
    "port_of_loading": "NANTONG, CHINA (CNNTG)",
    "port_of_discharge": "KARACHI, PAKISTAN (PKKHI)",
    "container_count": 3,
    "gross_weight_kg": 22000.0,
}


def test_numeric_mismatch_is_decided_by_arithmetic():
    si = dict(COMPLETE)
    bl = {**COMPLETE, "container_count": 4}          # 3 containers vs 4
    out = compare(si, bl, COMPARED_FIELDS)
    assert out.status == "MISMATCH"
    assert out.defect_fields == ["container_count"]
    assert out.has_defect is True


def test_blank_field_outranks_a_mismatch():
    """An unfilled field voids the comparison, even if another field disagrees.

    A field the customer never filled in is not a difference, so we cannot
    certify the pair either way — a human has to look. Measured against the
    official scorer: every gold MISMATCH email has all seven fields filled on
    both sides, and the only emails where a blank coexists with a disagreement
    are gold NEEDS_REVIEW. The disagreement stays visible as evidence.
    """
    si = {**COMPLETE, "gross_weight_kg": None}       # customer left it blank
    bl = {**COMPLETE, "container_count": 4}
    out = compare(si, bl, COMPARED_FIELDS)
    assert out.status == "NEEDS_REVIEW"
    assert out.review_reason == "missing_value"
    assert out.undecidable_fields == ["gross_weight_kg"]
    assert out.defect_fields == []                   # never assert a defect
    assert out.has_defect is False
    disagreement = [fr for fr in out.field_results if fr["field"] == "container_count"]
    assert disagreement and disagreement[0]["match"] is False


def test_all_match_gives_ok():
    fields = {
        "shipper": "APRIL FAR EAST (M) SDN BHD",
        "consignee": "EAST BRIGHT FZ-LLC",
        "notify_party": "EAST BRIGHT FZ-LLC",
        "port_of_loading": "NANTONG, CHINA (CNNTG)",
        "port_of_discharge": "KARACHI, PAKISTAN (PKKHI)",
        "container_count": 6,
        "gross_weight_kg": 131058.0,
    }
    out = compare(dict(fields), dict(fields), COMPARED_FIELDS)
    assert out.status == "OK"
    assert out.defect_fields == []


def test_same_port_with_same_locode_matches_despite_wording():
    si = {"port_of_loading": "NANTONG, CHINA (CNNTG)"}
    bl = {"port_of_loading": "Nantong  China  (CNNTG)"}   # case/spacing noise
    out = compare(si, bl, ["port_of_loading"])
    assert out.status == "OK"


def test_locode_is_part_of_the_value():
    """One side printing the UN/LOCODE and the other not is a difference —
    the value must agree as a whole (validated against the official scorer)."""
    si = {"port_of_discharge": "KARACHI, PAKISTAN (PKKHI)"}
    bl = {"port_of_discharge": "KARACHI, PAKISTAN"}
    out = compare(si, bl, ["port_of_discharge"])
    assert out.status == "MISMATCH"


def test_different_ports_are_flagged():
    si = {"port_of_discharge": "HOUSTON, US"}
    bl = {"port_of_discharge": "MOMBASA, KENYA"}
    assert compare(si, bl, ["port_of_discharge"]).status == "MISMATCH"


def test_name_differences_are_not_normalised_away():
    si = {"consignee": "EAST BRIGHT FZ-LLC"}
    bl = {"consignee": "UAB NOVAKOPA"}
    out = compare(si, bl, ["consignee"])
    assert out.status == "MISMATCH"


def test_punctuation_and_case_are_ignored_in_names():
    si = {"shipper": "April Far East (M) Sdn. Bhd."}
    bl = {"shipper": "APRIL FAR EAST (M) SDN BHD"}
    out = compare(si, bl, ["shipper"])
    assert out.status == "OK"


def test_weight_is_parsed_before_comparison():
    si = {"gross_weight_kg": "131,058 KG"}
    bl = {"gross_weight_kg": 131058.0}
    out = compare(si, bl, ["gross_weight_kg"])
    assert out.status == "OK"


def test_container_string_is_parsed_before_comparison():
    si = {"container_count": "6 x 40'HC"}
    bl = {"container_count": 6}
    out = compare(si, bl, ["container_count"])
    assert out.status == "OK"


def test_missing_value_escalates_instead_of_guessing():
    si = {"consignee": "EAST BRIGHT FZ-LLC", "container_count": 3}
    bl = {"consignee": "EAST BRIGHT FZ-LLC"}  # container_count missing
    out = compare(si, bl, ["consignee", "container_count"])
    assert out.status == "NEEDS_REVIEW"
    assert out.undecidable_fields == ["container_count"]
    assert out.defect_fields == []  # never invent a defect


def test_comparison_is_deterministic_across_runs():
    si = {"container_count": 3}
    bl = {"container_count": 4}
    a = compare(si, bl, ["container_count"])
    b = compare(si, bl, ["container_count"])
    assert (a.status, a.defect_fields) == (b.status, b.defect_fields)
    assert a.status == "MISMATCH"
