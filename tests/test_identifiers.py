"""Business identifiers read out of a document's own text (J1 / J2).

These are identity signals for grouping, not compared fields: the 7-field
comparison model and `extractor.LABELS` are deliberately untouched.
"""
from __future__ import annotations

from app.services import identifiers


def test_booking_reference_is_read_from_the_document():
    assert identifiers.extract_reference("Booking No: ABC123\nShipper: X\n") == "ABC123"
    assert identifiers.extract_reference("Shipment ID: SHP-001\n") == "SHP-001"
    assert identifiers.extract_reference("OC No.: 5RSG-00133\n") == "5RSG-00133"
    assert identifiers.extract_reference("B/L No.: MEDUUD104332\n") == "MEDUUD104332"
    assert identifiers.extract_reference("S/O No. 12345\n") == "12345"


def test_a_long_booking_reference_is_not_truncated():
    """Real references run long (MSDUL0942518196) — a 12-char cap clipped them."""
    assert identifiers.extract_reference("Booking Ref: MSDUL0942518196\n") == \
        "MSDUL0942518196"


def test_the_real_corpus_document_yields_its_reference_and_voyage():
    text = """SHIPPING INSTRUCTION
Shipper/Exporter: APRIL FAR EAST (M) SDN BHD
Port of Loading: PORT KLANG (WESTPORT), MALAYSIA (MYPKG)
Vessel Name: MMSS 2507 V.257087E
Booking Ref: MSDUL0942518196
"""
    found = identifiers.from_text(text)
    assert found.reference == "MSDUL0942518196"
    assert found.vessel == "MMSS 2507"
    assert found.voyage == "257087E"


def test_placeholders_and_prose_are_not_references():
    assert identifiers.extract_reference("Booking No:\nShipper: X\n") is None
    assert identifiers.extract_reference("Reference: TBA\n") is None
    assert identifiers.extract_reference("Please book a slot next week.\n") is None
    assert identifiers.extract_reference("") is None
    assert identifiers.from_text("nothing to see").is_empty


def test_iso_container_numbers_are_recognised():
    text = "Container No.: ABCU1234567\nSecond: MSKU7654321\n"
    assert identifiers.extract_containers(text) == ("ABCU1234567", "MSKU7654321")
    # Not an ISO container: the fourth letter must be U, J or Z.
    assert identifiers.extract_containers("Ref: ABCA1234567\n") == ()


def test_vessel_and_voyage_variants():
    assert identifiers.extract_vessel_voyage("Voy. No: 026E\n")[1] == "026E"
    assert identifiers.extract_vessel_voyage("Voyage: 118W\n")[1] == "118W"
    vessel, voyage = identifiers.extract_vessel_voyage("Vessel: EVER GIVEN V.012\n")
    assert vessel == "EVER GIVEN"
    assert voyage == "012"


def test_discriminators_are_stable_and_only_for_real_signals():
    ids = identifiers.from_text("Container No.: ABCU1234567\n")
    assert ids.discriminators() == ("CTR:ABCU1234567",)
    vessel_only = identifiers.from_text("Vessel: EVER GLORY\nVoy. No: 026E\n")
    assert vessel_only.discriminators() == ("VES:EVER GLORY:026E",)
    assert identifiers.from_text("Shipper: X\n").discriminators() == ()


def test_merge_combines_the_documents_of_one_shipment():
    si = identifiers.from_text("Container No.: ABCU1234567\nBooking No: AB-1\n")
    bl = identifiers.from_text("Voy. No: 026E\nVessel: EVER GLORY\n")
    merged = identifiers.merge(si, bl)
    assert merged.reference == "AB-1"
    assert merged.containers == ("ABCU1234567",)
    assert merged.voyage == "026E"
    assert merged.is_empty is False
