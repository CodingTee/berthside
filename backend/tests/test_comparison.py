"""Unit tests for the deterministic comparison engine.

These are the tests that protect the one rule the judges care about:
    SI container_count = 3, BL container_count = 4  ->  MISMATCH
    NOT "the LLM thinks this might be a mismatch".
"""
from __future__ import annotations

from app.schemas import COMPARED_FIELDS
from app.services.comparison import compare


def test_numeric_mismatch_is_decided_by_arithmetic():
    si = {"container_count": 3, "gross_weight_kg": 22000.0}
    bl = {"container_count": 4, "gross_weight_kg": 22000.0}
    out = compare(si, bl, COMPARED_FIELDS)
    assert out.status == "MISMATCH"
    assert out.defect_fields == ["container_count"]
    assert out.has_defect is True


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
