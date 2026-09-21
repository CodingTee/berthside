"""Tests for the document formats beyond PDF / Office / CSV.

Every format here is built as a real fixture rather than a mock, because the
whole point of the readers is that they handle the bytes a sender actually
produces. The fixtures are written with the same libraries the readers use
(``zipfile``, ``tarfile``, ``gzip``, ``py7zr``, ``email``) plus a small OLE2
writer in ``tests/ole_writer.py`` for ``.msg``, which has no writer on PyPI.

Covered: the container set (zip / 7z / rar / tar / gz), forwarded mail
(.eml / .msg), office documents (rtf / odt / ods / iWork), EDI (EDIFACT and
X12, with and without the interchange envelope), JSON, CAD (dxf / dwg),
HEIC photos, and the security gate's header rules for the new suffixes.
"""
from __future__ import annotations

import gzip
import io
import json
import sys
import tarfile
import zipfile
from email.message import EmailMessage
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from ole_writer import build_msg  # noqa: E402

from app.routers.ingest import expand_payloads  # noqa: E402
from app.services import (  # noqa: E402
    ai_service,
    archive,
    cad,
    doc_types,
    edi,
    extractor,
    json_doc,
    nested_mail,
    office_formats,
    workflow,
)
from app.services.security import verify_file_safety  # noqa: E402


# --------------------------------------------------------------- document text

SI_TEXT = (
    "SHIPPING INSTRUCTION\n"
    "Shipper: KELVIN SHIPPING PTE LTD\n"
    "Consignee: AVERIS TRADING SDN BHD\n"
    "Notify Party: AVERIS LOGISTICS SDN BHD\n"
    "Port of Loading: SINGAPORE (SGSIN)\n"
    "Port of Discharge: PORT KLANG (MYPKG)\n"
    "Container Count: 2 x 40HC\n"
    "Gross Weight (KG): 24,960.00\n"
)

SI_FIELDS = {
    "shipper": "KELVIN SHIPPING PTE LTD",
    "consignee": "AVERIS TRADING SDN BHD",
    "notify_party": "AVERIS LOGISTICS SDN BHD",
    "port_of_loading": "SINGAPORE (SGSIN)",
    "port_of_discharge": "PORT KLANG (MYPKG)",
}


def assert_si_fields(text: str) -> None:
    """The extractor reads every compared field out of a rendered SI."""
    result = extractor.extract_fields(text, "SI")
    assert not result.missing, f"missing {result.missing} in:\n{text}"
    for field, expected in SI_FIELDS.items():
        assert result.fields.get(field) == expected, f"{field}={result.fields.get(field)!r}"


# ------------------------------------------------------------------ containers

def _zip_bytes(members: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in members.items():
            zf.writestr(name, data)
    return buf.getvalue()


def _tar_bytes(members: dict[str, bytes], mode: str = "w") -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode=mode) as tf:
        for name, data in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def _seven_zip_bytes(members: dict[str, bytes]) -> bytes:
    py7zr = pytest.importorskip("py7zr")
    buf = io.BytesIO()
    with py7zr.SevenZipFile(buf, "w") as zf:
        for name, data in members.items():
            zf.writestr(data, name)
    return buf.getvalue()


def test_zip_expands_and_classifies():
    payload = _zip_bytes({"docs/SHP-001_SI.txt": SI_TEXT.encode()})
    assert archive.detect_container("pack.zip", payload) == "zip"
    expansion = archive.expand_archive("pack.zip", payload)
    assert [name for name, _ in expansion.members] == ["SHIP-001_SI.txt"] or \
        [name for name, _ in expansion.members] == ["SHP-001_SI.txt"]
    name, data = expansion.members[0]
    assert data == SI_TEXT.encode()
    assert doc_types.detect_si_bl(name) == "SI"


def test_tar_gz_and_plain_gz_expand():
    inner = {"SHP-002_BL.txt": b"BILL OF LADING\nShipper: ACME LTD\n"}
    tgz = _tar_bytes(inner, mode="w:gz")
    assert archive.detect_container("bundle.tgz", tgz) == "gzip"
    got = dict(archive.expand_archive("bundle.tgz", tgz).members)
    assert got["SHP-002_BL.txt"].startswith(b"BILL OF LADING")

    # A gzip stream is not a tar, so its member takes the name of the file with
    # the compression suffix removed. The doc-type layer reads that name, so a
    # bare stream that is demonstrably text is named for what it is rather than
    # left extension-less and therefore unclassifiable.
    plain = gzip.compress(SI_TEXT.encode())
    assert archive.detect_container("SHP-003_SI.gz", plain) == "gzip"
    members = dict(archive.expand_archive("SHP-003_SI.gz", plain).members)
    assert list(members) == ["SHP-003_SI.txt"]
    assert doc_types.detect_si_bl("SHP-003_SI.txt") == "SI"


def test_seven_zip_expands():
    payload = _seven_zip_bytes({"SHP-004_SI.txt": SI_TEXT.encode()})
    assert archive.detect_container("pack.7z", payload) == "7z"
    members = dict(archive.expand_archive("pack.7z", payload).members)
    assert members["SHP-004_SI.txt"] == SI_TEXT.encode()


def test_rar_reports_a_missing_tool_instead_of_failing_silently():
    """RAR needs an external extractor; when there is none it must say so."""
    rar = b"Rar!\x1a\x07\x01\x00" + b"\x00" * 64
    assert archive.detect_container("pack.rar", rar) == "rar"
    expansion = archive.expand_archive("pack.rar", rar)
    if archive._rar_available():
        # A tool exists, so the malformed body is what stops it, and the reader
        # still reports rather than raising.
        assert not expansion.members
        assert expansion.notes
    else:
        assert not expansion.members
        assert any("RAR needs an external extractor" in n for n in expansion.notes)


def test_package_documents_are_not_swallowed_as_zip():
    """A .docx or .odt is a zip, but it is a document, not a carrier."""
    docx = _zip_bytes({"[Content_Types].xml": b"<Types/>", "word/document.xml": b"<w:body/>"})
    assert archive.detect_container("report.docx", docx) is None
    odt = _zip_bytes({"mimetype": b"application/vnd.oasis.opendocument.text",
                      "content.xml": b"<office:document-content/>"})
    assert archive.detect_container("note.odt", odt) is None


def test_archive_member_traversal_is_refused_and_reported():
    payload = _zip_bytes({"../../../etc/passwd": b"escape", "SHP-005_SI.txt": b"ok"})
    expansion = archive.expand_archive("evil.zip", payload)
    names = [name for name, _ in expansion.members]
    assert "SHIP-005_SI.txt" not in names
    assert all(".." not in n for n in names)
    assert names == ["SHP-005_SI.txt"]
    assert any("passwd" in note for note in expansion.notes), expansion.notes


def test_nesting_spends_one_shared_budget():
    """Wrapping a payload in containers must not multiply the size caps."""
    inner = _zip_bytes({"SHP-006_SI.txt": SI_TEXT.encode()})
    outer = _zip_bytes({"inner.zip": inner})
    budget = archive.Budget()
    first = archive.expand_archive("outer.zip", outer, budget)
    assert [n for n, _ in first.members] == ["inner.zip"]
    second = archive.expand_archive("inner.zip", first.members[0][1], budget)
    assert [n for n, _ in second.members] == ["SHP-006_SI.txt"]

    spent = archive.Budget()
    spent.members_left = 0
    assert not archive.expand_archive("outer.zip", outer, spent).members


def test_expand_payloads_recurses_through_a_container():
    zip_bytes = _zip_bytes({"SHP-007_SI.txt": SI_TEXT.encode()})
    documents, alerts = expand_payloads([("SHP-007.zip", zip_bytes)])
    assert [n for n, _ in documents] == ["SHP-007_SI.txt"]
    assert not alerts
    text, _ = ai_service.document_text(*documents[0])
    assert_si_fields(text)


# -------------------------------------------------------------- nested messages

def _eml_bytes(attachments: dict[str, bytes], body: str = "Please find the SI below.\n") -> bytes:
    msg = EmailMessage()
    msg["From"] = "shipper@fastocean.com"
    msg["To"] = "operations@shipsync.demo"
    msg["Subject"] = "FW: SHP-008 shipping documents"
    msg.set_content(body)
    for name, data in attachments.items():
        if name.endswith(".txt"):
            msg.add_attachment(data, maintype="text", subtype="plain", filename=name)
        else:
            msg.add_attachment(data, maintype="application", subtype="pdf", filename=name)
    return msg.as_bytes()


def _msg_bytes(attachments: dict[str, bytes],
               body: str = "Please find the SI below.\n") -> bytes:
    """An Outlook ``.msg`` carrying the same attachments as ``_eml_bytes``."""
    return build_msg(
        subject="FW: shipping documents",
        sender="Kelvin Tan",
        sender_address="shipper@fastocean.com",
        to="operations@shipsync.demo",
        body=body,
        attachments=[(name, data) for name, data in attachments.items()],
    )


def test_eml_yields_attachments_plus_a_synthetic_body():
    raw = _eml_bytes({"SHP-008_SI.txt": SI_TEXT.encode()})
    assert nested_mail.detect_mail("forward.eml", raw) == "eml"
    expansion = nested_mail.expand_mail("forward.eml", raw)
    members = dict(expansion.members)
    assert members["SHP-008_SI.txt"] == SI_TEXT.encode()

    body_name = nested_mail.body_document_name("forward.eml")
    assert body_name.endswith(doc_types.SYNTHETIC_SUFFIX + ".txt")
    assert doc_types.is_synthetic(body_name)
    assert b"Please find the SI below." in members[body_name]


def test_eml_attachment_with_a_traversing_name_is_refused():
    raw = _eml_bytes({"../../../etc/passwd": b"escape"})
    expansion = nested_mail.expand_mail("forward.eml", raw)
    assert all(".." not in name for name, _ in expansion.members)
    assert expansion.notes or expansion.blocked


def test_msg_is_read_from_a_real_ole2_file():
    """``.msg`` has no writer on PyPI, so the fixture is built here."""
    payload = ("BILL OF LADING\n" "Shipper: KELVIN SHIPPING PTE LTD\n").encode()
    raw = build_msg(
        subject="FW: SHP-009 documents",
        sender="Kelvin Tan",
        sender_address="kelvin@kelvinshipping.com.sg",
        to="ops@averis.com",
        body="SI attached for SHP-009.\n",
        attachments=[("SHP-009_SI.txt", SI_TEXT.encode()),
                     ("SHP-009_BL.txt", payload)],
    )
    assert raw.startswith(nested_mail.OLE_MAGIC)
    assert nested_mail.detect_mail("forward.msg", raw) == "msg"

    members = dict(nested_mail.expand_mail("forward.msg", raw).members)
    assert members["SHP-009_SI.txt"] == SI_TEXT.encode()
    assert members["SHP-009_BL.txt"] == payload
    body_name = nested_mail.body_document_name("forward.msg")
    assert b"SI attached for SHP-009." in members[body_name]


def test_msg_property_streams_are_matched_without_regard_to_case():
    """Outlook spells the hex in upper case, and ``001F`` is the unicode type.

    The fixture and the reader once shared the opposite belief, so every test
    passed while every real ``.msg`` read as empty: both agreed the text lives in
    ``__substg1.0_1000001e``, and no Outlook message has ever written that.
    """
    raw = _msg_bytes({}, body=SI_TEXT)
    assert "__substg1.0_1000001F".encode("utf-16-le") in raw

    members = dict(nested_mail.expand_mail("FW_SHP-008_SI.msg", raw).members)
    body = members[nested_mail.body_document_name("FW_SHP-008_SI.msg")].decode()
    assert "KELVIN SHIPPING PTE LTD" in body, body
    assert "Kelvin Tan" in body, body


def test_msg_payload_survives_the_regular_sector_path():
    """Attachments above the 4KB mini-stream cutoff must come back byte exact."""
    big = b"%PDF-1.4\n" + b"padding " * 900 + b"\n%%EOF\n"
    raw = build_msg(subject="big", sender="A", to="B", body="x",
                    attachments=[("SHP-010_BL.pdf", big)])
    members = dict(nested_mail.expand_mail("big.msg", raw).members)
    assert members["SHP-010_BL.pdf"] == big


def test_message_not_mistaken_for_prose():
    """A plain text document must never be claimed by the message reader."""
    for name, data in (("SHP-011_SI.txt", SI_TEXT.encode()),
                       ("SHP-011_SI", SI_TEXT.encode())):
        assert not nested_mail.is_nested_mail(name, data)
    # ... while a header block with no extension still is.
    raw = b"From: a@b.com\r\nSubject: hi\r\n\r\nbody"
    assert nested_mail.is_nested_mail("forward", raw)


def test_zip_of_a_forwarded_message_expands_both_levels():
    inner = _eml_bytes({"SHP-012_SI.txt": SI_TEXT.encode()})
    documents, alerts = expand_payloads([("SHP-012.zip", _zip_bytes({"fwd.eml": inner}))])
    names = [n for n, _ in documents]
    assert "SHP-012_SI.txt" in names
    assert any(n.endswith(doc_types.SYNTHETIC_SUFFIX + ".txt") for n in names)
    assert not alerts


# ------------------------------------------------------------- office documents

def _rtf_table(rows: list[tuple[str, str]]) -> bytes:
    body = ""
    for label, value in rows:
        body += (r"\trowd\cellx3000\cellx6000\pard\intbl "
                 + label + r"\cell " + value + r"\cell\row" + "\n")
    return (r"{\rtf1\ansi\ansicpg1252\deff0{\fonttbl{\f0\fnil Calibri;}}"
            r"{\*\generator Riched20}\viewkind4\uc1 "
            r"\pard\b SHIPPING INSTRUCTION\b0\par" + "\n"
            + body + "}").encode("cp1252")


def test_rtf_table_becomes_readable_label_lines():
    raw = _rtf_table([
        ("Shipper", "KELVIN SHIPPING PTE LTD"),
        ("Consignee", "AVERIS TRADING SDN BHD"),
        ("Notify Party", "AVERIS LOGISTICS SDN BHD"),
        ("Port of Loading", "SINGAPORE (SGSIN)"),
        ("Port of Discharge", "PORT KLANG (MYPKG)"),
        ("Container Count", "2 x 40HC"),
        ("Gross Weight (KG)", "24,960.00"),
    ])
    assert office_formats.detect_office_format("SHP-013_SI.rtf", raw) == "rtf"
    text, source = office_formats.read_office_text("SHP-013_SI.rtf", raw)
    assert source == "rtf"
    # Control words and font tables are structure, not content.
    assert "Calibri" not in text and "Riched20" not in text
    assert_si_fields(text)


def _odf_bytes(content_xml: str, mimetype: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("mimetype", mimetype)
        zf.writestr("content.xml", content_xml)
    return buf.getvalue()


_ODF_HEAD = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<office:document-content '
    'xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" '
    'xmlns:table="urn:oasis:names:tc:opendocument:xmlns:table:1.0" '
    'xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0">'
    "<office:body>"
)


def test_ods_table_cells_are_joined_as_label_value():
    rows = [("Shipper", "KELVIN SHIPPING PTE LTD"),
            ("Consignee", "AVERIS TRADING SDN BHD"),
            ("Notify Party", "AVERIS LOGISTICS SDN BHD"),
            ("Port of Loading", "SINGAPORE (SGSIN)"),
            ("Port of Discharge", "PORT KLANG (MYPKG)"),
            ("Container Count", "2 x 40HC"),
            ("Gross Weight (KG)", "24,960.00")]
    xml = _ODF_HEAD + "<office:spreadsheet><table:table>" + "".join(
        "<table:table-row>"
        f"<table:table-cell><text:p>{a}</text:p></table:table-cell>"
        f"<table:table-cell><text:p>{b}</text:p></table:table-cell>"
        "</table:table-row>" for a, b in rows
    ) + "</table:table></office:spreadsheet></office:body></office:document-content>"

    raw = _odf_bytes(xml, "application/vnd.oasis.opendocument.spreadsheet")
    assert office_formats.detect_office_format("SHP-014_SI.ods", raw) == "ods"
    text, source = office_formats.read_office_text("SHP-014_SI.ods", raw)
    assert source == "odf-ods"
    assert_si_fields(text)


def test_odt_paragraphs_and_repeated_spaces():
    xml = (_ODF_HEAD + "<office:text>"
           "<text:p>SHIPPING INSTRUCTION</text:p>"
           "<table:table><table:table-row>"
           "<table:table-cell><text:p>Port of Loading</text:p></table:table-cell>"
           '<table:table-cell><text:p>SINGAPORE<text:s text:c="2"/>(SGSIN)</text:p>'
           "</table:table-cell></table:table-row></table:table>"
           "</office:text></office:body></office:document-content>")
    raw = _odf_bytes(xml, "application/vnd.oasis.opendocument.text")
    text, source = office_formats.read_office_text("SHP-015_SI.odt", raw)
    assert source == "odf-odt"
    assert "SHIPPING INSTRUCTION" in text
    assert "SINGAPORE (SGSIN)" in text


def test_iwork_document_yields_its_quicklook_preview():
    pdf = (b"%PDF-1.4\n1 0 obj<</Type/Catalog>>endobj\ntrailer<</Root 1 0 R>>\n%%EOF\n")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("Index/Document.iwa", b"\x00\x01\x02 protobuf")
        zf.writestr("QuickLook/Preview.pdf", pdf)
    raw = buf.getvalue()
    assert office_formats.detect_office_format("SHP-016_SI.pages", raw) == "pages"
    assert office_formats.iwork_preview_pdf(raw) == pdf


def _snappy_literal(payload: bytes) -> bytes:
    """A Snappy block storing ``payload`` as one literal run.

    Compressing is not what a fixture is for, so this emits the simplest block
    Snappy allows instead of carrying a real encoder into the test suite. The
    readers only ever decode.
    """
    out = bytearray()
    value = len(payload)
    while True:                                  # varint of the output length
        byte = value & 0x7F
        value >>= 7
        out.append(byte | (0x80 if value else 0))
        if not value:
            break
    size = len(payload) - 1
    if size < 60:
        out.append(size << 2)
    else:
        extra = max(1, (size.bit_length() + 7) // 8)
        out.append((59 + extra) << 2)
        out += size.to_bytes(extra, "little")
    out += payload
    return bytes(out)


def _iwork_bytes(text: str) -> bytes:
    """An iWork package whose ``Index/`` store holds ``text``.

    Deliberately without a ``QuickLook/Preview.pdf``: the preview is Finder's,
    and a test that needs macOS to render one proves nothing on the machine this
    suite actually runs on.
    """
    member = b"\x00\x00\x00\x00" + _snappy_literal(text.encode("utf-8"))
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("Index/Document.iwa", member)
    return buf.getvalue()


def test_iwork_text_is_read_from_the_package_store():
    """The package's own Snappy/protobuf store is the route, not the preview."""
    raw = _iwork_bytes(SI_TEXT)
    assert office_formats.detect_office_format("SHP-016_SI.pages", raw) == "pages"
    text, source = office_formats.read_office_text("SHP-016_SI.pages", raw)
    assert source == "iwork-pages"
    assert_si_fields(text)
    assert extractor.extract_fields(text, "SI").fields["container_count"] == 2


def test_iwork_store_keeps_only_field_lines():
    """A stylesheet shares the store, so runs are filtered by the label.

    Returning every printable run would hand the extractor far more text than
    the document contains, and a style named like a field would read as a value.
    """
    noise = ("Application/White/Standard\nTitle & Subtitle\nen_US\n"
             "Lorem Ipsum Dolor\nTransition")
    assert office_formats.read_office_text("SHP-016_SI.pages",
                                           _iwork_bytes(noise)) is None


# ------------------------------------------------------------------- EDI

_EDIFACT = "\n".join([
    "UNA:+.? '",
    "UNB+UNOC:3+SGSHP:172:20+AVERIS:172:20+260315:1200+MSG047'",
    "UNH+1+IFTMIN:D:99B:UN:2.1'",
    "BGM+610+SHIP047+9'",
    "DTM+137:20260315:102'",
    "NAD+SH+SGSHP:172:20+KELVIN SHIPPING PTE LTD+12 KEPPEL ROAD'",
    "NAD+CN+MYAVR:172:20+AVERIS TRADING SDN BHD+LOT 5 JALAN KLANG'",
    "NAD+NI+MYAVR:172:20+AVERIS LOGISTICS SDN BHD'",
    "LOC+9+SINGAPORE:SGSIN:139:6'",
    "LOC+11+PORT KLANG:MYPKG:139:6'",
    "MEA+WT+AAD+KGM:24960'",
    "EQD+CN+MSKU1234567'",
    "EQD+CN+MSKU7654321'",
    "UNZ+1+MSG047'",
]).encode("latin-1")


def test_edifact_structured_segments_map_to_labels():
    assert edi.detect_edi("msg.edi", _EDIFACT) == "edifact"
    parsed = edi.parse_edi("msg.edi", _EDIFACT)
    assert not parsed.has_free_text
    assert_si_fields("\n".join(parsed.lines))
    assert extractor.extract_fields("\n".join(parsed.lines), "SI").fields["container_count"] == 2


def test_edifact_free_text_only_message_is_read():
    """A sender who wraps their own layout in FTX sends no structured segments."""
    lines = SI_TEXT.strip().split("\n")
    raw = ("UNH+1+IFTMIN:D:99B:UN'\n"
           + "".join(f"FTX+AAA+++{ln}'\n" for ln in lines)
           + "UNZ+1+MSG'\n").encode("utf-8")
    assert edi.detect_edi("msg.edi", raw) == "edifact"
    parsed = edi.parse_edi("msg.edi", raw)
    assert parsed.has_free_text
    assert_si_fields("\n".join(parsed.lines))


def test_edifact_release_character_keeps_punctuation():
    raw = ("UNH+1+IFTMIN:D:99B:UN'\n"
           "FTX+AAA+++Shipper?: KELVIN SHIPPING PTE LTD'\n"
           "FTX+AAA+++Consignee?: AVERIS TRADING SDN BHD'\n"
           "FTX+AAA+++Notify Party?: AVERIS LOGISTICS SDN BHD'\n"
           "FTX+AAA+++Port of Loading?: SINGAPORE (SGSIN)'\n"
           "FTX+AAA+++Port of Discharge?: PORT KLANG (MYPKG)'\n"
           "FTX+AAA+++Container Count?: 2 x 40HC'\n"
           "FTX+AAA+++Gross Weight (KG)?: 24,960.00'\n"
           "UNZ+1+MSG'\n").encode("utf-8")
    parsed = edi.parse_edi("msg.edi", raw)
    assert_si_fields("\n".join(parsed.lines))


def test_edifact_utf8_is_not_mangled():
    raw = ("UNH+1+IFTMIN:D:99B:UN'\n"
           "FTX+AAA+++Shipper: KELVIN SHIPPING PTE LTD'\n"
           "FTX+AAA+++Gross Weight 毛重(KGS): 24,960.00'\n"
           "UNZ+1+MSG'\n").encode("utf-8")
    parsed = edi.parse_edi("msg.edi", raw)
    assert "毛重" in "\n".join(parsed.lines)


def test_edifact_weight_in_pounds_is_converted():
    raw = _EDIFACT.replace(b"MEA+WT+AAD+KGM:24960", b"MEA+WT+AAD+LBR:55000")
    parsed = edi.parse_edi("msg.edi", raw)
    text = "\n".join(parsed.lines)
    assert "24,947" in text or "24,948" in text, text
    assert any("converted from LBR" in n for n in parsed.notes), parsed.notes


_X12 = (
    "ISA*00*          *00*          *ZZ*SGSHP          *ZZ*AVERIS         "
    "*260315*1200*U*00401*000000047*0*P*>~"
    "GS*IM*SGSHP*AVERIS*20260315*1200*47*X*004010~"
    "ST*204*0001~"
    "B2*SHIP047**SGSHP**PP*PP~"
    "N1*SH*KELVIN SHIPPING PTE LTD*92*SGSHP~"
    "N3*12 KEPPEL ROAD~N4*SINGAPORE*SG*089057~"
    "N1*CN*AVERIS TRADING SDN BHD*92*MYAVR~"
    "N3*LOT 5 JALAN KLANG~N4*PORT KLANG*MY*42000~"
    "N1*NI*AVERIS LOGISTICS SDN BHD~"
    "R4*1*K*SGSIN*SINGAPORE*SG~R4*2*K*MYPKG*PORT KLANG*MY~"
    "MEA*WT*G*24960*KG~"
    "N7*MSKU1234567*4*20~N7*MSKU7654321*4*20~"
    "SE*18*0001~GE*1*47~IEA*1*000000047~"
).encode("latin-1")


def test_x12_structured_segments_map_to_labels():
    assert edi.detect_edi("load.x12", _X12) == "x12"
    parsed = edi.parse_edi("load.x12", _X12)
    assert_si_fields("\n".join(parsed.lines))
    assert extractor.extract_fields("\n".join(parsed.lines), "SI").fields["container_count"] == 2


def test_x12_without_the_isa_envelope_is_read():
    """A transaction set pulled out of a batch has no ISA to read delimiters from."""
    body = _X12.split(b"GS*", 1)[1]
    body = b"GS*" + body
    body = body.split(b"ST*", 1)[0] + b"ST*" + body.split(b"ST*", 1)[1]
    # Drop the ISA segment entirely.
    no_isa = b"\r\n".join(line for line in body.split(b"~") if not line.startswith(b"ISA"))
    assert edi.detect_edi("load.x12", no_isa) == "x12"
    parsed = edi.parse_edi("load.x12", no_isa)
    assert_si_fields("\n".join(parsed.lines))


def test_x12_nte_free_text_only_message_is_read():
    raw = ("ST*404*0001~\r\n"
           + "".join(f"NTE*GEN*{ln}~\r\n" for ln in SI_TEXT.strip().split("\n"))
           + "SE*10*0001~\r\n").encode("utf-8")
    assert edi.detect_edi("bl.x12", raw) == "x12"
    parsed = edi.parse_edi("bl.x12", raw)
    assert parsed.has_free_text
    assert_si_fields("\n".join(parsed.lines))


def test_edi_refuses_prose():
    assert edi.detect_edi("notes.txt", b"just a note about shipping") is None
    assert edi.parse_edi("notes.txt", b"just a note about shipping") is None


# ------------------------------------------------------------------- JSON

def test_json_object_is_flattened_into_labels():
    document = {
        "shipment": {
            "shipper": {"name": "KELVIN SHIPPING PTE LTD"},
            "consignee": {"name": "AVERIS TRADING SDN BHD"},
            "notifyParty": "AVERIS LOGISTICS SDN BHD",
            "portOfLoading": "SINGAPORE (SGSIN)",
            "portOfDischarge": "PORT KLANG (MYPKG)",
            "containerCount": 2,
            "grossWeightKg": 24960.0,
        }
    }
    raw = json.dumps(document).encode()
    assert json_doc.detect_json("SHP-017_SI.json", raw) == "json"
    text, source = json_doc.read_json_text("SHP-017_SI.json", raw)
    assert source == "json"
    assert_si_fields(text)


def test_jsonl_is_read_line_by_line():
    raw = (b'{"Shipper": "KELVIN SHIPPING PTE LTD"}\n'
           b'{"Consignee": "AVERIS TRADING SDN BHD"}\n'
           b'{"Notify Party": "AVERIS LOGISTICS SDN BHD"}\n'
           b'{"Port of Loading": "SINGAPORE (SGSIN)"}\n'
           b'{"Port of Discharge": "PORT KLANG (MYPKG)"}\n'
           b'{"Container Count": "2 x 40HC"}\n'
           b'{"Gross Weight (KG)": "24,960.00"}\n')
    text, source = json_doc.read_json_text("SHP-018_SI.jsonl", raw)
    assert source == "jsonl"
    assert_si_fields(text)


def test_json_empty_or_null_only_is_refused():
    assert json_doc.read_json_text("x.json", b"{}") is None
    assert json_doc.read_json_text("x.json", json.dumps({"a": None, "b": True}).encode()) is None
    assert json_doc.detect_json("x.json", b"not json at all") is None


# -------------------------------------------------------------------- CAD

def _dxf(pairs: list[tuple[str, str]]) -> bytes:
    out = []
    for code, value in pairs:
        out.append(f"{code}\n{value}\n")
    return "".join(out).encode("latin-1")


def test_dxf_text_entities_are_read():
    pairs = [(0, "SECTION"), (2, "ENTITIES")]
    for line in SI_TEXT.strip().split("\n"):
        pairs += [(0, "TEXT"), (1, line)]
    pairs += [(0, "ENDSEC"), (0, "EOF")]
    raw = _dxf(pairs)
    assert cad.detect_cad("SHP-019_SI.dxf", raw) == "dxf"
    text, source = cad.read_cad_text("SHP-019_SI.dxf", raw)
    assert source == "dxf"
    assert_si_fields(text)


def test_dxf_attribute_tags_are_paired_with_their_values():
    raw = _dxf([
        (0, "SECTION"), (2, "ENTITIES"),
        (0, "ATTRIB"), (2, "Shipper"), (1, "KELVIN SHIPPING PTE LTD"),
        (0, "ATTRIB"), (2, "Consignee"), (1, "AVERIS TRADING SDN BHD"),
        (0, "ATTRIB"), (2, "Notify Party"), (1, "AVERIS LOGISTICS SDN BHD"),
        (0, "ATTRIB"), (2, "Port of Loading"), (1, "SINGAPORE (SGSIN)"),
        (0, "ATTRIB"), (2, "Port of Discharge"), (1, "PORT KLANG (MYPKG)"),
        (0, "ATTRIB"), (2, "Container Count"), (1, "2"),
        (0, "ATTRIB"), (2, "Gross Weight"), (1, "24,960.00 KG"),
        (0, "ENDSEC"), (0, "EOF"),
    ])
    text, _ = cad.read_cad_text("SHP-020_SI.dxf", raw)
    assert_si_fields(text)


def test_binary_dxf_and_prose_are_refused():
    assert cad.read_cad_text("b.dxf", b"AutoCAD Binary DXF\r\n\x1a\x00\x01") is None
    assert cad.read_cad_text("x.dxf", b"just a note about shipping " * 20) is None


def test_dwg_needs_evidence_before_it_is_claimed():
    """A DWG scavenge must not invent a document out of stray strings."""
    thin = b"AC1027" + b"\x00" + b"SHIPPER: SOMEONE LTD" + b"\x00" * 20
    assert cad.read_cad_text("thin.dwg", thin) is None
    assert cad.detect_cad("thin.dwg", thin) == "dwg"

    blob = b"AC1027\x00\x00"
    for line in SI_TEXT.strip().split("\n"):
        blob += b"\x00" + line.encode() + b"\x00" + b"\xfe\xff" * 8
    text, source = cad.read_cad_text("stowage.dwg", blob)
    # The source names the route, not just the format: a line recovered from a
    # binary object stream is less trustworthy than one read from an ASCII
    # drawing, and the verdict shows the operator which one produced it.
    assert source == "dwg-scavenge"
    assert_si_fields(text)


# --------------------------------------------------------------- HEIC photos

def _heic_bytes() -> bytes:
    pillow_heif = pytest.importorskip("pillow-heif")
    from PIL import Image, ImageDraw

    img = Image.new("RGB", (900, 200), "white")
    ImageDraw.Draw(img).text((20, 40), "SHIPPING INSTRUCTION", fill="black")
    buf = io.BytesIO()
    img.save(buf, format="HEIF", quality=100)
    return buf.getvalue()


def test_heic_is_detected_by_magic_not_just_name():
    from app.services import photo

    raw = _heic_bytes()
    assert photo.looks_like_heif(raw)
    assert photo.is_heif("IMG_4821.HEIC", raw)
    assert photo.is_heif("photo.heif", raw)
    assert not photo.looks_like_heif(b"not an image at all")


def test_heic_is_normalised_to_png_for_the_readers():
    from app.services import photo

    raw = _heic_bytes()
    name, data = photo.normalize_image("IMG_4821.HEIC", raw)
    assert name.lower().endswith(".png")
    assert data.startswith(b"\x89PNG")
    # The normalised payload must satisfy the gate that rejected the original.
    assert verify_file_safety(name, data)[0]


# ------------------------------------------------------------ security gate

@pytest.mark.parametrize("name,payload", [
    ("photo.heic", b"\x00\x00\x00\x18ftypheic\x00\x00\x00\x00"),
    ("photo.heif", b"\x00\x00\x00\x18ftypmif1\x00\x00\x00\x00"),
    ("note.rtf", b"{\\rtf1\\ansi hello}"),
    ("msg.eml", b"From: a@b.com\r\nSubject: hi\r\n\r\nbody"),
    ("stowage.dxf", b"0\nSECTION\n2\nENTITIES\n"),
    ("plan.dwg", b"AC1027\x00\x00"),
])
def test_new_formats_pass_the_gate_when_they_look_right(name, payload):
    ok, reason = verify_file_safety(name, payload)
    assert ok, reason


@pytest.mark.parametrize("name,payload", [
    ("photo.heic", b"not an image at all"),
    ("note.rtf", b"plain text, no control words"),
    ("msg.eml", b"no headers here"),
    ("plan.dwg", b"PK\x03\x04abcdef"),
])
def test_new_formats_are_blocked_when_the_header_lies(name, payload):
    ok, reason = verify_file_safety(name, payload)
    assert not ok
    assert reason != "SAFE"


# -------------------------------------------------------------- doc type map

@pytest.mark.parametrize("name,expected", [
    ("SHP-001_SI.eml", "SI"),
    ("SHP-001_BL.msg", "BL"),
    ("SHP-001_SI.rtf", "SI"),
    ("SHP-001_BL.odt", "BL"),
    ("SHP-001_SI.ods", "SI"),
    ("SHP-001_BL.pages", "BL"),
    ("SHP-001_SI.edi", "SI"),
    ("SHP-001_BL.edifact", "BL"),
    ("SHP-001_SI.x12", "SI"),
    ("SHP-001_BL.json", "BL"),
    ("SHP-001_SI.jsonl", "SI"),
    ("SHP-001_BL.dxf", "BL"),
    ("SHP-001_SI.dwg", "SI"),
    ("SHP-001_BL.heic", "BL"),
    ("SHP-001_BL.heif", "BL"),
])
def test_new_suffixes_classify_as_shipping_documents(name, expected):
    assert doc_types.detect_si_bl(name) == expected


# ------------------------------------------------------- end to end by format

@pytest.mark.parametrize("name,payload", [
    ("SHP-030_SI.zip", _zip_bytes({"SHP-030_SI.txt": SI_TEXT.encode()})),
    ("SHP-030_SI.tar", _tar_bytes({"SHP-030_SI.txt": SI_TEXT.encode()})),
    ("SHP-030_SI.gz", gzip.compress(SI_TEXT.encode())),
    ("SHP-030_SI.rtf", _rtf_table([tuple(l.split(": ", 1)) for l in SI_TEXT.strip().split("\n")[1:]])),
    ("SHP-030_SI.ods", _odf_bytes(
        _ODF_HEAD + "<office:spreadsheet><table:table>" + "".join(
            "<table:table-row>"
            f"<table:table-cell><text:p>{l.split(': ', 1)[0]}</text:p></table:table-cell>"
            f"<table:table-cell><text:p>{l.split(': ', 1)[1]}</text:p></table:table-cell>"
            "</table:table-row>"
            for l in SI_TEXT.strip().split("\n")[1:])
        + "</table:table></office:spreadsheet></office:body></office:document-content>",
        "application/vnd.oasis.opendocument.spreadsheet")),
    ("SHP-030_SI.edi", ("UNH+1+IFTMIN:D:99B:UN'\n"
                        + "".join(f"FTX+AAA+++{l}'\n" for l in SI_TEXT.strip().split("\n"))
                        + "UNZ+1+MSG'\n").encode("utf-8")),
    ("SHP-030_SI.x12", ("ST*404*0001~\r\n"
                        + "".join(f"NTE*GEN*{l}~\r\n" for l in SI_TEXT.strip().split("\n"))
                        + "SE*10*0001~\r\n").encode("utf-8")),
    ("SHP-030_SI.json", json.dumps({l.split(": ", 1)[0]: l.split(": ", 1)[1]
                                    for l in SI_TEXT.strip().split("\n")[1:]}).encode()),
    ("SHP-030_SI.dxf", _dxf(
        [(0, "SECTION"), (2, "ENTITIES")]
        + [pair for l in SI_TEXT.strip().split("\n") for pair in ((0, "TEXT"), (1, l))]
        + [(0, "ENDSEC"), (0, "EOF")])),
    ("SHP-030_SI.eml", _eml_bytes({"SHP-030_SI.txt": SI_TEXT.encode()})),
    ("SHP-030_SI.msg", _msg_bytes({"SHP-030_SI.txt": SI_TEXT.encode()})),
    ("SHP-030_SI.pages", _iwork_bytes(SI_TEXT)),
    ("SHP-030_SI.numbers", _iwork_bytes(SI_TEXT)),
    ("SHP-030_SI.key", _iwork_bytes(SI_TEXT)),
], ids=[
    "zip", "tar", "gz", "rtf", "ods", "edi", "x12", "json", "dxf",
    "eml", "msg", "pages", "numbers", "key",
])
def test_every_format_reaches_the_extractor(name, payload):
    """The one contract that matters: a document in any supported format is
    expanded, read, and yields the same compared fields.

    The document is selected the way the pipeline selects it, because a
    forwarded message yields several: the body becomes a synthetic document
    alongside the files that were attached, and only a real attachment may be
    compared. Reading every member into one blob would let the covering note
    outvote the shipping instruction it forwarded.
    """
    documents, alerts = expand_payloads([(name, payload)])
    assert not alerts, alerts
    assert documents, f"{name} produced no document"

    readable: dict[str, str] = {}
    for member, data in documents:
        text, source = ai_service.document_text(member, data)
        assert source != "unreadable", f"{member} was not readable"
        assert text, member
        readable[member] = text

    chosen = next((m for m in readable
                   if not doc_types.is_synthetic(m) and doc_types.detect_si_bl(m) == "SI"),
                  None)
    assert chosen, f"{name}: no real SI document among {sorted(readable)}"

    result = extractor.extract_fields(readable[chosen], "SI")
    assert not result.missing, f"{name}: missing {result.missing} in:\n{readable[chosen]}"
    for field, expected in SI_FIELDS.items():
        assert result.fields.get(field) == expected, f"{name}: {field}={result.fields.get(field)!r}"
    assert result.fields.get("container_count") == 2, name


def test_a_forwarded_body_never_displaces_the_real_attachment():
    """The synthetic body is a fallback: a real file always answers for its
    kind, and the body is only reached once no real file does."""
    expansion = nested_mail.expand_mail(
        "FW_SHP-008_SI.eml", _eml_bytes({"SHP-008_SI.txt": SI_TEXT.encode()}))
    names = [m for m, _ in expansion.members]
    body = nested_mail.body_document_name("FW_SHP-008_SI.eml")
    assert body in names and "SHP-008_SI.txt" in names
    assert doc_types.is_synthetic(body)

    # The attached file answers for SI even though the body also claims it.
    si_name, _ = workflow._find_doc_attachments({"attachments": names})
    assert si_name == "SHP-008_SI.txt", si_name

    # With the file gone the body is all there is, so it is used instead.
    picked = workflow._find_doc_attachments({"attachments": [body]})
    assert picked[0] == body, picked

    # A body that inherits a stem carrying no kind is left unclaimed rather than
    # guessed at: a wrong guess reads as a discrepancy the customer never made.
    untyped = nested_mail.body_document_name("FW_SHP-008.eml")
    assert workflow._find_doc_attachments({"attachments": [untyped]}) == (None, None)
