"""Tests for Legacy Office formats (.xls, .doc) and Image / Multi-page TIFF documents.

Verifies:
1. Excel 97-2003 (.xls) parsing via xlrd.
2. Word 97-2003 (.doc) parsing via olefile and openxml fallback.
3. Multi-page TIFF (.tiff) OCR reading across multiple frames.
4. Single image (.png) OCR reading.
5. Integration into ai_service.extract_document.
"""
from __future__ import annotations

import io
from pathlib import Path
from PIL import Image, ImageDraw

from app.services import ai_service, extractor
from app.services.ocr import ocr_image


def test_xls_extraction():
    """Verify that Excel 97-2003 (.xls) content can be parsed."""
    # We can test with a synthetic .xls stream or mock xlrd workbook
    import xlrd
    # Build in-memory workbook representation test
    from unittest.mock import MagicMock, patch

    mock_sheet = MagicMock()
    mock_sheet.nrows = 4
    mock_sheet.ncols = 2
    mock_sheet.cell_value.side_effect = lambda r, c: [
        ["Shipper:", "APRIL FINE PAPER TRADING"],
        ["Port of Loading:", "NANTONG, CHINA (CNNTG)"],
        ["Total Containers:", "3 x 40'HC"],
        ["Gross Weight:", "22,000 KG"],
    ][r][c]

    mock_wb = MagicMock()
    mock_wb.sheets.return_value = [mock_sheet]

    with patch("xlrd.open_workbook", return_value=mock_wb):
        res = ai_service.extract_document("SI", "booking_123_si.xls", b"\xd0\xcf\x11\xe0FakeXlsBytes")
        assert res.readable is True
        assert res.source == "xls"
        assert "APRIL FINE PAPER" in res.fields["shipper"]
        assert "NANTONG" in res.fields["port_of_loading"]
        assert res.fields["container_count"] == 3
        assert res.fields["gross_weight_kg"] == 22000.0


def test_doc_ole_extraction():
    """Verify that Word 97-2003 (.doc) OLE streams are safely parsed without crashing."""
    from unittest.mock import MagicMock, patch

    doc_text_stream = (
        b"\x00\x00\x00\x00"
        b"Shipper: ACME TRADING LTD\r\n"
        b"Consignee: GLOBAL BUYER INC\r\n"
        b"Port of Loading: SHANGHAI, CHINA (CNSHA)\r\n"
        b"Port of Discharge: ROTTERDAM (NLRTM)\r\n"
        b"Containers: 5 x 40HC\r\n"
        b"Gross Weight: 35000 kg\r\n"
    )

    mock_ole = MagicMock()
    mock_ole.exists.return_value = True
    mock_stream = MagicMock()
    mock_stream.read.return_value = doc_text_stream
    mock_ole.openstream.return_value = mock_stream

    with patch("olefile.isOleFile", return_value=True), \
         patch("olefile.OleFileIO") as mock_ole_cls:
        mock_ole_cls.return_value.__enter__.return_value = mock_ole
        res = ai_service.extract_document("SI", "instruction_si.doc", b"dummy_doc_content")
        assert res.readable is True
        assert res.source == "doc-ole"
        assert "ACME TRADING" in res.fields["shipper"]
        assert "SHANGHAI" in res.fields["port_of_loading"]
        assert res.fields["container_count"] == 5


def test_doc_ole_reads_utf16_text():
    """Word 97 stores text as UTF-16LE, and reading it as latin-1 destroyed it.

    Decoding the stream as latin-1 leaves a NUL between every character, so the
    label regexes saw "S h i p p e r" and matched nothing. The result still
    counted as readable, which sent the case to the comparison with all seven
    fields empty and blamed the sender for a document we had failed to read.
    """
    from unittest.mock import MagicMock, patch

    si_text = (
        "SHIPPING INSTRUCTION\n"
        "Shipper: APRIL FAR EAST (M) SDN BHD\n"
        "Consignee: EAST BRIGHT FZ-LLC\n"
        "Port of Loading: NANTONG, CHINA (CNNTG)\n"
        "Port of Discharge: KARACHI, PAKISTAN (PKKHI)\n"
        "Total Containers: 6 x 40'HC\n"
        "Gross Weight: 131,058 KG\n"
    )
    # A real stream interleaves binary structures with 2-byte text runs.
    stream = b"\xec\xa5\xc1\x00" * 6 + si_text.encode("utf-16-le") + b"\x00\x00"

    mock_ole = MagicMock()
    mock_ole.exists.return_value = True
    mock_stream = MagicMock()
    mock_stream.read.return_value = stream
    mock_ole.openstream.return_value = mock_stream

    with patch("olefile.isOleFile", return_value=True), \
         patch("olefile.OleFileIO") as mock_ole_cls:
        mock_ole_cls.return_value.__enter__.return_value = mock_ole
        res = ai_service.extract_document("SI", "legacy_si.doc", b"dummy")

    assert res.readable is True, res.raw_text[:200]
    assert res.source == "doc-ole"
    assert "APRIL FAR EAST" in res.fields["shipper"]
    assert res.fields["container_count"] == 6


def test_doc_ole_refuses_to_guess_from_binary_noise():
    """Without enough evidence the honest verdict is `unreadable`, not a guess.

    A value invented out of binary noise is indistinguishable from a real
    discrepancy, so a stream the reader can barely decode must stop at a human.
    """
    from unittest.mock import MagicMock, patch

    noise = bytes(range(1, 32)) * 40

    mock_ole = MagicMock()
    mock_ole.exists.return_value = True
    mock_stream = MagicMock()
    mock_stream.read.return_value = noise
    mock_ole.openstream.return_value = mock_stream

    with patch("olefile.isOleFile", return_value=True), \
         patch("olefile.OleFileIO") as mock_ole_cls:
        mock_ole_cls.return_value.__enter__.return_value = mock_ole
        res = ai_service.extract_document("SI", "garbled_si.doc", b"dummy")

    assert res.readable is False
    assert res.fields == {}


def test_multipage_tiff_ocr():
    """Verify that multi-page TIFF images are traversed and OCR'd frame by frame."""
    # Create a 2-page TIFF: Page 1 has Shipper, Page 2 has Containers & Weight
    img1 = Image.new("RGB", (600, 150), color="white")
    d1 = ImageDraw.Draw(img1)
    d1.text((20, 30), "Shipper: PACIFIC ASIA TRADING", fill="black")
    d1.text((20, 70), "Port of Loading: NANTONG (CNNTG)", fill="black")

    img2 = Image.new("RGB", (600, 150), color="white")
    d2 = ImageDraw.Draw(img2)
    d2.text((20, 30), "Port of Discharge: ROTTERDAM (NLRTM)", fill="black")
    d2.text((20, 70), "Total Containers: 3 x 40HC", fill="black")

    buf = io.BytesIO()
    img1.save(buf, format="TIFF", save_all=True, append_images=[img2])
    tiff_bytes = buf.getvalue()

    # Extract via OCR
    text = ocr_image(tiff_bytes, "fax_bl.tiff")
    assert text is not None
    assert "PACIFIC ASIA TRADING" in text.upper() or "PACIFIC" in text.upper()
    assert "ROTTERDAM" in text.upper()

    # Verify integration with ai_service.extract_document
    res = ai_service.extract_document("BL", "fax_document_bl.tiff", tiff_bytes)
    assert res.readable is True
    assert res.source == "image-ocr"
    assert "port_of_loading" in res.fields or "port_of_discharge" in res.fields


def test_single_image_png_ocr():
    """Verify that single image files (.png / .jpg) are recognized and OCR'd."""
    img = Image.new("RGB", (500, 100), color="white")
    d = ImageDraw.Draw(img)
    d.text((20, 35), "Shipper: EVERGREEN LOGISTICS", fill="black")

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    png_bytes = buf.getvalue()

    res = ai_service.extract_document("SI", "mobile_photo_si.png", png_bytes)
    assert res.readable is True
    assert res.source == "image-ocr"
    assert "EVERGREEN" in str(res.fields.get("shipper", "")).upper()
