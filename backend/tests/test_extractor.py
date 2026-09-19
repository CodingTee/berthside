"""Field extraction tests — label wording varies wildly between SI and BL."""
from __future__ import annotations

from app.services.extractor import extract_fields, normalize

SI = """SHIPPING INSTRUCTION
========================================

Shipper: APRIL FAR EAST (M) SDN BHD
  TOWER 2, AVENUE 5, LEVEL 6
Consignee (Non-Negotiable): EAST BRIGHT FZ-LLC
  RAKEZ AMENITY CENTER
Notify: EAST BRIGHT FZ-LLC
Port of Loading (POL): NANTONG, CHINA (CNNTG)
POD: KARACHI, PAKISTAN (PKKHI)
Total Containers: 6 x 40'HC
Gross Wt (kgs): 131,058 KG
"""

BL = """BILL OF LADING (DRAFT)
========================================

SHIPPER: APRIL FAR EAST (M) SDN BHD
  TOWER 2, AVENUE 5, LEVEL 6
To the Order of: UAB NOVAKOPA
  RAKEZ AMENITY CENTER
Notify Party: UAB NOVAKOPA
Load Port: NANTONG, CHINA (CNNTG)
Port of Discharge: KARACHI, PAKISTAN (PKKHI)
Container Count: 6 x 40'HC
Gross Weight (KG): 131,058 KG
"""


def test_si_fields_are_fully_extracted():
    r = extract_fields(SI, "SI")
    assert r.missing == []
    assert r.fields["shipper"] == "APRIL FAR EAST (M) SDN BHD"
    assert r.fields["consignee"] == "EAST BRIGHT FZ-LLC"
    assert r.fields["container_count"] == 6
    assert r.fields["gross_weight_kg"] == 131058.0


def test_bl_uses_different_labels_for_the_same_fields():
    """'To the Order of' is the consignee; 'Load Port' is the loading port."""
    r = extract_fields(BL, "BL")
    assert r.missing == []
    assert r.fields["consignee"] == "UAB NOVAKOPA"
    assert r.fields["port_of_loading"].startswith("NANTONG")


def test_combined_label_is_not_mistaken_for_consignee():
    """'Notify Party/Intermediate Consignee' belongs to notify_party
    (the label that starts earliest wins)."""
    text = "Notify Party/Intermediate Consignee: TOPKOPY MIDDLE EAST FZE\nConsignee: EAST BRIGHT FZ-LLC\n"
    r = extract_fields(text, "SI")
    assert r.fields["notify_party"] == "TOPKOPY MIDDLE EAST FZE"
    assert r.fields["consignee"] == "EAST BRIGHT FZ-LLC"


def test_chinese_characters_between_label_and_unit_are_tolerated():
    text = "Gross Weight毛重(KGS): 67,311 KG\n"
    r = extract_fields(text, "SI")
    assert r.fields["gross_weight_kg"] == 67311.0


def test_or_packages_label_variant():
    text = "No. of Containers or Packages: 1 x 40'HC\n"
    r = extract_fields(text, "SI")
    assert r.fields["container_count"] == 1


def test_hyphenated_company_name_survives():
    text = "Consignee: EAST BRIGHT FZ-LLC\n"
    r = extract_fields(text, "SI")
    assert r.fields["consignee"] == "EAST BRIGHT FZ-LLC"


def test_table_style_rows_without_colons():
    text = ("SHIPPER | APRIL FAR EAST (M) SDN BHD\n"
            "Container Count | 6 x 40HC\n"
            "Gross Weight | 131,058 KG\n")
    r = extract_fields(text, "SI")
    assert r.fields["shipper"] == "APRIL FAR EAST (M) SDN BHD"
    assert r.fields["container_count"] == 6
    assert r.fields["gross_weight_kg"] == 131058.0


def test_binary_content_is_reported_as_unreadable():
    r = extract_fields("\x00\x01\x02\xff\xfe garbage", "SI")
    assert r.readable is False


def test_normalize_strips_noise_consistently():
    assert normalize("shipper", "April Far East (M) Sdn. Bhd.") == \
        normalize("shipper", "APRIL FAR EAST M SDN BHD")
    assert normalize("container_count", "6 x 40'HC") == 6
    # a port keeps BOTH its name and its UN/LOCODE — the whole string is the value
    assert normalize("port_of_loading", "NANTONG, CHINA (CNNTG)") == \
        "NANTONG CHINA CNNTG"
    assert normalize("port_of_loading", "Nantong, China  (CNNTG)") == \
        normalize("port_of_loading", "NANTONG, CHINA (CNNTG)")


def test_address_is_not_part_of_the_company_name():
    assert normalize("consignee",
                     "ROXCEL TRADING GMBH | OPERNRING 3-5; 1010 VIENNA") == \
        normalize("consignee", "ROXCEL TRADING GMBH")


def test_legal_form_differences_do_not_create_a_defect():
    """Same company, different legal suffix / punctuation → still a match."""
    assert normalize("shipper", "ROXCEL TRADING GMBH") == \
        normalize("shipper", "ROXCEL TRADING GmbH")


def test_different_companies_stay_different():
    assert normalize("consignee", "EAST BRIGHT FZ-LLC") != \
        normalize("consignee", "UAB NOVAKOPA")


# --------------------------------------------------------------------------
# Robustness: noise that must NOT change the answer (scripts/stress_evaluate.py)
# --------------------------------------------------------------------------
def test_non_breaking_and_doubled_spaces_do_not_hide_labels():
    """PDF exports use NBSP and column padding; labels must still resolve."""
    clean = "Port of Loading: NANTONG, CHINA (CNNTG)\nGross Weight (KG): 21,577 KG"
    noisy = clean.replace(" ", "\u00a0").replace("\n", "\n")
    padded = "Port  of  Loading: NANTONG, CHINA (CNNTG)\nGross  Weight (KG): 21,577 KG"

    base = extract_fields(clean, "SI")
    for variant in (noisy, padded):
        got = extract_fields(variant, "SI")
        assert got.fields["port_of_loading"] == base.fields["port_of_loading"]
        assert got.fields["gross_weight_kg"] == base.fields["gross_weight_kg"]


def test_weights_are_not_glued_to_the_following_text():
    """21577 kg must not degrade to 21 kg when the value is followed by words."""
    assert extract_fields(
        "Gross Weight (KG): 21,577 KG\nVessel Name: MMSS 2507", "SI"
    ).fields["gross_weight_kg"] == 21577.0
    assert extract_fields(
        "Gross Weight (KG): 131 058 KG Vessel Name: X", "BL"
    ).fields["gross_weight_kg"] == 131058.0


def test_a_document_collapsed_onto_one_line_is_still_parsed():
    """Some extractors drop line breaks; every field must still be found."""
    doc = (
        "SHIPPING INSTRUCTION "
        "Shipper: APRIL FAR EAST (M) SDN BHD "
        "Consignee: EAST BRIGHT FZ-LLC "
        "Notify Party: EAST BRIGHT FZ-LLC "
        "Port of Loading (POL): NANTONG, CHINA (CNNTG) "
        "Port of Discharge (POD): KARACHI, PAKISTAN (PKKHI) "
        "Total Containers: 6 x 40'HC "
        "Gross Weight (KG): 131,058 KG "
        "Vessel Name: MMSS 2507 "
        "Booking Ref: MSDUL0942518196 "
        "HS Code: 48025600 "
        "Description of Goods: PAPERONE DIGITAL COPIER PAPER"
    )
    got = extract_fields(doc, "SI")
    assert got.missing == [], f"missing: {got.missing}"
    assert got.fields["port_of_discharge"] == "KARACHI, PAKISTAN (PKKHI)"
    assert got.fields["container_count"] == 6
    assert got.fields["gross_weight_kg"] == 131058.0


def test_slash_combined_label_is_not_split_on_a_mega_line():
    """'Notify Party/Intermediate Consignee' is ONE label, not two fields.

    Regression guard: splitting it made notify_party take the next segment's
    first word ("CONSIGNEE") and produced a false defect on a real xlsx pair.
    """
    row = (
        "Notify Party/Intermediate Consignee | 3S PAPER PRODUCTS SDN BHD | "
        "NO 12, JALAN INDUSTRI 3/6; RAWANG INDUSTRIAL PARK; 48000 RAWANG"
    )
    got = extract_fields(row, "BL")
    assert got.fields["notify_party"] == "3S PAPER PRODUCTS SDN BHD"


# --------------------------------------------------------------------------
# Advanced: complex documents + OCR (scanned / image-only PDFs)
# --------------------------------------------------------------------------
def test_extraction_records_which_reader_produced_the_text():
    """`source` tells a reviewer whether a decision came from pixels or text."""
    assert extract_fields("Shipper: ACME LTD", "SI").source == ""


# --------------------------------------------------------------------------
# Fields the customer left unfilled: blank / TBA / ____ placeholders
# --------------------------------------------------------------------------
def test_placeholder_port_is_not_a_value():
    """"____MT" is a blank the customer never filled, not a port name.

    Keeping it would compare "____MT" against "SINGAPORE (SGSIN)" and report a
    discrepancy the customer never made; the honest answer is "undecidable".
    """
    r = extract_fields("Port of Loading (POL): ____MT\nPOD: TBA\n", "SI")
    assert "port_of_loading" in r.missing
    assert "port_of_discharge" in r.missing
    assert r.fields.get("port_of_loading") is None
    assert r.is_complete is False


def test_na_and_underscore_weight_placeholders_are_not_values():
    r = extract_fields("Port of Discharge (POD): N/A\nGross Weight (KG): _______ MTS\n", "SI")
    assert "port_of_discharge" in r.missing
    assert "gross_weight_kg" in r.missing


def test_blank_label_does_not_swallow_the_next_field():
    """A blank "CONSIGNEE:" followed by "Notify: X" means empty — not X.

    Regression guard: the next-line fallback used to grab the following
    *label* line as this field's value, silently filling a blank the customer
    left open (real SI: email_520).
    """
    r = extract_fields("Shipper/Exporter: APRIL FINE PAPER TRADING\n"
                       "CONSIGNEE: \n"
                       "Notify: CLIFFORD PAPER INC\n", "SI")
    assert r.fields.get("consignee") is None
    assert "consignee" in r.missing
    assert r.fields["notify_party"] == "CLIFFORD PAPER INC"


def test_next_line_value_still_works_when_it_is_not_a_label():
    """The fallback must keep working for genuine continuation lines."""
    r = extract_fields("Port of Loading (POL)\nNHAVA SHEVA, INDIA\n", "SI")
    assert r.fields["port_of_loading"] == "NHAVA SHEVA, INDIA"


def test_company_name_containing_a_label_word_is_kept():
    """A value that merely *contains* a label word is not a label line."""
    r = extract_fields("Shipper: CONTAINER CORPORATION OF INDIA\n", "SI")
    assert r.fields["shipper"] == "CONTAINER CORPORATION OF INDIA"


def test_garbage_bytes_never_become_a_document():
    """A corrupt PDF must escalate, never yield a confident empty answer."""
    from app.services import ai_service
    assert ai_service._read_pdf(b"not a pdf at all", "SI") is None


def test_ocr_groups_words_into_reading_order_lines():
    """OCR returns loose boxes; they must be reassembled top-to-bottom."""
    from app.services.ocr import _items_to_lines
    items = [
        ("Port", 0.9, 100.0, 10.0), ("of", 0.9, 100.0, 60.0),
        ("Loading:", 0.9, 100.0, 90.0),
        ("Shipper:", 0.9, 20.0, 10.0), ("ACME", 0.9, 20.0, 90.0),
        ("noise", 0.1, 200.0, 10.0),      # below confidence threshold
    ]
    lines = _items_to_lines(items).splitlines()
    assert lines[0] == "Shipper: ACME"
    assert lines[1] == "Port of Loading:"
    assert "noise" not in _items_to_lines(items)


def test_ocr_engine_reads_rendered_text_when_installed():
    """End-to-end OCR smoke test — skipped when the toolchain is absent."""
    from app.services import ocr
    if not ocr.ocr_available():
        import pytest
        pytest.skip("OCR toolchain not installed")
    from PIL import Image, ImageDraw
    img = Image.new("RGB", (900, 200), "white")
    draw = ImageDraw.Draw(img)
    draw.text((20, 40), "Gross Weight (KG): 21,577 KG", fill="black")
    draw.text((20, 110), "Port of Loading: NANTONG", fill="black")
    engine = ocr._engine()
    items = engine(img)
    assert items, "OCR engine returned nothing for a clean synthetic image"
    text = ocr._items_to_lines(items)
    assert "21577" in text.replace(",", "") or "21,577" in text
