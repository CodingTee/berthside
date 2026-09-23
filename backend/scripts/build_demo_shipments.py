"""Build the curated multi-shipment demo dataset.

Why this exists
---------------
The primary corpus (``backend/data/corpus``, 520 emails) is what the engine is
**scored** on, so it must never be edited. This script builds a second, much
smaller and deliberately designed dataset whose job is to *demonstrate* the
behaviours the future shipment-centred Dashboard needs:

    SHP-001  complete shipment, all fields agree           -> VERIFIED
    SHP-002  SI + invoice, no BL                           -> missing-document
    SHP-003  SI v1 -> v2 -> v3, BL agrees with v3 only     -> version control
    SHP-004  SI/BL disagree on container count             -> mismatch
    SHP-005  several emails from several senders, ZIP      -> grouping
    SHP-006  scanned (TIFF) BL                             -> OCR path

It also exercises every file format the reader can actually parse: TXT, CSV,
XML, XLSX, DOCX, PDF, PNG/JPEG/TIFF (via OCR) and ZIP (expanded before
classification). Formats that cannot be verified here (legacy ``.doc``/``.xls``)
are deliberately absent rather than faked.

Output layout (all committed, so the demo does not depend on running this):

    backend/demo-shipments/emails.json        sent to POST /api/process
    backend/demo-shipments/attachments/*      the real binary documents
    backend/demo-shipments/expectations.json  what each shipment should resolve to

Usage
-----
    python scripts/build_demo_shipments.py
    python scripts/build_demo_shipments.py --out backend/demo-shipments
"""
from __future__ import annotations

import argparse
import io
import json
import zipfile
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parent.parent

SENDER = {
    "forwarder": ("Amelia Tan", "amelia.tan@nordicpaper-asia.com"),
    "carrier": ("Ops Desk", "ops.desk@globalshippingline.com"),
    "docs": ("Docs Team", "export.docs@nordicpaper-asia.com"),
    "accounts": ("Invoicing", "ar@nordicpaper-asia.com"),
    "agent": ("Port Agent", "agent@hamoverflow.de"),
}
TO = "operations@shipsync.demo"


# --------------------------------------------------------------------------- #
# document bodies                                                             #
# --------------------------------------------------------------------------- #
def _si_doc(ref, *, shipper, consignee, notify, pol, pod, containers, weight,
            vessel, voyage, booking, goods, container_no):
    return f"""SHIPPING INSTRUCTION
========================================

Shipper/Exporter: {shipper}
  2 JALAN SULTAN, PELABUHAN UTARA, 42000 PORT KLANG, MALAYSIA
CONSIGNEE: {consignee}
  AM SANDTOR 12, HAFEN CITY, GERMANY
NOTIFY PARTY: {notify}
Port of Loading: {pol}
Discharge Port: {pod}
No. of Containers or Packages: {containers}
Gross Weight (KG): {weight}
Vessel Name: {vessel}
Voy. No: {voyage}
Container No.: {container_no}
Kinds of Packages; Description of Goods: {goods}
Booking Ref: {booking}
OC No.: {ref}
Freight: PREPAID
"""


def _bl_doc(ref, *, shipper, consignee, notify, pol, pod, containers, weight,
            vessel, voyage, booking, goods, container_no, bl_no):
    return f"""BILL OF LADING (DRAFT)
========================================

SHIPPER: {shipper}
  2 JALAN SULTAN, PELABUHAN UTARA, 42000 PORT KLANG, MALAYSIA
CONSIGNEE: {consignee}
  AM SANDTOR 12, HAFEN CITY, GERMANY
Notify: {notify}
Port of Loading (POL): {pol}
POD: {pod}
Container Count: {containers}
Gross Wt (kgs): {weight}
Vessel Name: {vessel}
Voyage: {voyage}
Container No.: {container_no}
Commodity: {goods}
Bill of Lading No.: {bl_no}
Booking Ref: {booking}
OC No.: {ref}
Freight: PREPAID
"""


def _invoice_doc(ref, *, number, total, currency="USD", goods="AUTOMOTIVE SPARE PARTS"):
    return f"""COMMERCIAL INVOICE
========================================

Invoice No.: {number}
Shipment Ref: {ref}
Description: {goods}
Total Amount: {currency} {total}
Payment Terms: NET 30
"""


# --------------------------------------------------------------------------- #
# PDF rendering (multi-page aware)                                            #
# --------------------------------------------------------------------------- #
def _render_pdf(pages: list[str], dpi: int = 200) -> bytes:
    """Render text blocks to a real multi-page PDF.

    Requires PyMuPDF, which is only needed to (re)generate this dataset — the
    produced files are committed, so neither the app nor the image build needs
    it.
    """
    import fitz  # type: ignore

    doc = fitz.open()
    for block in pages:
        page = doc.new_page()
        y = 60
        for line in block.splitlines():
            page.insert_text((60, y), line, fontsize=9.5, fontname="cour")
            y += 15
    data = doc.tobytes(deflate=True)
    doc.close()
    return data


def _render_image(pdf_bytes: bytes, suffix: str, dpi: int = 200) -> bytes:
    """Rasterise a PDF page the way a fax/scan would, so OCR has to do the work."""
    import fitz  # type: ignore
    from PIL import Image

    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    pix = doc[0].get_pixmap(dpi=dpi)
    img = Image.open(io.BytesIO(pix.tobytes("png")))
    # Scans are greyscale: keeping them RGB quadruples the file with no
    # information gain (an uncompressed RGB TIFF here is ~5MB vs ~150KB).
    img = img.convert("L")
    buf = io.BytesIO()
    fmt = suffix.lstrip(".").lower()
    img.save(buf, format={"png": "PNG", "jpg": "JPEG", "jpeg": "JPEG",
                          "tif": "TIFF", "tiff": "TIFF"}[fmt],
             **({"compression": "tiff_lzw"} if fmt in ("tif", "tiff") else {}))
    doc.close()
    return buf.getvalue()


def _xlsx(rows: list[tuple[str, str]]) -> bytes:
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    for row in rows:
        ws.append(list(row))
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _docx(text: str) -> bytes:
    from docx import Document

    doc = Document()
    for line in text.splitlines():
        doc.add_paragraph(line)
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


def _csv(rows: list[tuple[str, str]]) -> bytes:
    """Write real CSV: values containing commas must be quoted or they split.

    Hand-joinining with commas corrupted exactly the two fields that carry
    thousands separators — "Gross Weight (KG):,24,800 KG" read as three cells
    and the weight came back as 24 kg.
    """
    import csv as _csv

    buf = io.StringIO()
    _csv.writer(buf, quoting=_csv.QUOTE_MINIMAL).writerows(
        [label, value] for label, value in rows)
    return buf.getvalue().encode()


def _xml(tag_map: list[tuple[str, str]], root: str = "ShippingInstruction") -> bytes:
    body = "\n".join(f"  <{tag}>{value}</{tag}>" for tag, value in tag_map)
    return f'<?xml version="1.0" encoding="UTF-8"?>\n<{root}>\n{body}\n</{root}>\n'.encode()


def _zip(members: list[tuple[str, bytes]]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in members:
            zf.writestr(name, data)
    return buf.getvalue()


# --------------------------------------------------------------------------- #
# scenarios                                                                   #
# --------------------------------------------------------------------------- #
def build(out_dir: Path) -> dict:
    attachments: dict[str, bytes] = {}
    emails: list[dict] = []

    def add_email(idx, ref, sender_key, subject, body, files, offset_minutes=0):
        name, addr = SENDER[sender_key]
        emails.append({
            "email_id": f"demo-{idx:03d}",
            "message_id": f"demo-{idx:03d}",
            "thread_id": f"demo-{ref.lower()}",
            "from": addr,
            "from_name": name,
            "to": TO,
            "subject": subject,
            "body": body,
            "received": f"2026-09-20T{8 + offset_minutes // 60:02d}:{offset_minutes % 60:02d}:00",
            "attachments": [
                {"filename": fname, "content_base64": ""} for fname in files
            ],
        })

    # ---------------------------------------------------------------- SHP-001
    # Complete, clean, and the BL is a two-page PDF whose decisive fields sit on
    # page 2: extraction must not stop at the cover page.
    ref = "SHP-001"
    base = dict(
        shipper="NORDIC PAPER ASIA SDN BHD",
        consignee="HAMBURG RECYCLING GMBH",
        notify="HAMBURG RECYCLING GMBH",
        pol="PORT KLANG, MALAYSIA (MYPKG)",
        pod="HAMBURG, GERMANY (DEHAM)",
        containers="2 x 40'HC",
        weight="41,200 KG",
        vessel="EVER GLORY",
        voyage="026E",
        booking="MSDU100000001",
        goods="120 CARTONS AUTOMOTIVE SPARE PARTS",
        container_no="ABCU1234567",
    )
    attachments["SHP-001_SI.pdf"] = _render_pdf([_si_doc(ref, **base)])
    cover = (f"SCANNED COPY - PAGE 1 OF 2\n{ref}\nBOOKING REF: MSDU100000001\n"
             "FIELDS CONTINUE ON THE NEXT PAGE\n")
    attachments["SHP-001_BL.pdf"] = _render_pdf(
        [cover, _bl_doc(ref, **base, bl_no="MEDUUD100001")])
    attachments["SHP-001_Invoice.pdf"] = _render_pdf(
        [_invoice_doc(ref, number="INV-2026-0001", total="182,400.00")])
    add_email(
        1, ref, "forwarder",
        f"{ref} - please check SI and draft BL",
        "Attached are the SI and draft BL for this shipment. Please check the "
        "details and confirm. Commercial invoice is attached too.",
        ["SHP-001_SI.pdf", "SHP-001_BL.pdf", "SHP-001_Invoice.pdf"], 0)

    # ---------------------------------------------------------------- SHP-002
    # The documented "missing BL" case: SI (CSV) + invoice (XLSX), no BL ever.
    ref = "SHP-002"
    b2 = dict(base)
    b2.update(weight="24,800 KG", containers="1 x 20'GP", booking="MSDU100000002",
              container_no="ABCU7654321", vessel="MSC LUCIA", voyage="118W",
              pol="TANJUNG PELEPAS, MALAYSIA (MYTPP)",
              pod="ROTTERDAM, NETHERLANDS (NLRTM)",
              shipper="ASIA CABLE MANUFACTURING SDN BHD",
              consignee="ROTTERDAM CABLE BV",
              notify="ROTTERDAM CABLE BV",
              goods="48 DRUMS COPPER CABLE")
    attachments["SHP-002_SI.csv"] = _csv([
        ("SHIPPING INSTRUCTION", ref),
        ("Shipper:", b2["shipper"]),
        ("Consignee:", b2["consignee"]),
        ("Notify Party:", b2["notify"]),
        ("Port of Loading:", b2["pol"]),
        ("Discharge Port:", b2["pod"]),
        ("No. of Containers or Packages:", b2["containers"]),
        ("Gross Weight (KG):", b2["weight"]),
        ("Vessel Name:", b2["vessel"]),
        ("Voy. No:", b2["voyage"]),
        ("Booking Ref:", b2["booking"]),
    ])
    attachments["SHP-002_Invoice.xlsx"] = _xlsx([
        ("COMMERCIAL INVOICE", ""),
        ("Invoice No.:", "INV-2026-0002"),
        ("Shipment Ref:", ref),
        ("Shipper:", b2["shipper"]),
        ("Consignee:", b2["consignee"]),
        ("Total Amount:", "USD 96,400.00"),
    ])
    add_email(
        2, ref, "forwarder",
        f"Shipping Instruction {ref} - please verify, BL still outstanding",
        "Attached are the shipping instruction and the commercial invoice. The "
        "carrier has not released the bill of lading yet - please check what we "
        "have so far.",
        ["SHP-002_SI.csv", "SHP-002_Invoice.xlsx"], 25)

    # ---------------------------------------------------------------- SHP-003
    # Three SI revisions. Only the newest agrees with the BL, so comparing
    # against anything but the current version would report a false mismatch.
    ref = "SHP-003"
    v1 = dict(base, weight="38,900 KG", booking="MSDU100000003",
              container_no="ABCU5550001", vessel="COSCO HARMONY", voyage="039E",
              pol="PORT KLANG, MALAYSIA (MYPKG)",
              pod="ANTWERP, BELGIUM (BEANR)",
              shipper="DELTA AUTO PARTS (M) SDN BHD",
              consignee="ANTWERP MOTORS NV",
              notify="ANTWERP MOTORS NV",
              goods="95 CARTONS AUTOMOTIVE COMPONENTS")
    attachments["SHP-003_SI_v1.txt"] = _si_doc(ref, **v1)
    v2 = dict(v1, weight="40,150 KG")
    attachments["SHP-003_SI_v2.txt"] = _si_doc(ref, **v2)
    v3 = dict(v2, weight="41,500 KG", containers="3 x 40'HC")
    attachments["SHP-003_SI_v3.docx"] = _docx(_si_doc(ref, **v3))
    attachments["SHP-003_BL.txt"] = _bl_doc(
        ref, **v3, bl_no="MEDUUD100003")
    attachments["SHP-003_Invoice.pdf"] = _render_pdf(
        [_invoice_doc(ref, number="INV-2026-0003", total="204,750.00")])
    add_email(3, ref, "docs",
              f"{ref} shipping instruction (first issue) - please check",
              "Attached is the first shipping instruction, please check it. "
              "Details may still change - the packing list is not final.",
              ["SHP-003_SI_v1.txt"], 40)
    add_email(4, ref, "docs",
              f"REVISED Shipping Instruction {ref} v2 (weight updated) - please check",
              "Please check the revised SI and discard v1. Gross weight revised "
              "after the final weighbridge reading.",
              ["SHP-003_SI_v2.txt"], 55)
    add_email(5, ref, "forwarder",
              f"FINAL Shipping Instruction {ref} v3 + Bill of Lading",
              "Final version (v3) supersedes v1 and v2: one extra container and "
              "the confirmed weight. Please compare against the attached BL.",
              ["SHP-003_SI_v3.docx", "SHP-003_BL.txt",
               "SHP-003_Invoice.pdf"], 70)

    # ---------------------------------------------------------------- SHP-004
    # Genuine disagreement: SI says 3 containers, the BL says 4.
    ref = "SHP-004"
    b4 = dict(base, weight="52,300 KG", booking="MSDU100000004",
              container_no="ABCU8880002", vessel="EVER GIVEN", voyage="112W",
              pol="PENANG, MALAYSIA (MYPEN)",
              pod="FELIXSTOWE, UNITED KINGDOM (GBFXT)",
              shipper="PENANG SEMICON SDN BHD",
              consignee="FELIXSTOWE ELECTRONICS LTD",
              notify="FELIXSTOWE ELECTRONICS LTD",
              goods="210 CARTONS ELECTRONIC COMPONENTS")
    attachments["SHP-004_SI.xml"] = _xml([
        ("Reference", ref),
        ("Shipper", b4["shipper"]),
        ("Consignee", b4["consignee"]),
        ("NotifyParty", b4["notify"]),
        ("PortOfLoading", b4["pol"]),
        ("DischargePort", b4["pod"]),
        ("NoOfContainersOrPackages", "3 x 40'HC"),
        ("GrossWeightKG", b4["weight"]),
        ("VesselName", b4["vessel"]),
        ("VoyNo", b4["voyage"]),
    ])
    attachments["SHP-004_BL.txt"] = _bl_doc(
        ref, **{**b4, "containers": "4 x 40'HC"}, bl_no="MEDUUD100004")
    attachments["SHP-004_Invoice.docx"] = _docx(
        _invoice_doc(ref, number="INV-2026-0004", total="318,900.00",
                     goods="210 CARTONS ELECTRONIC COMPONENTS"))
    add_email(6, ref, "forwarder",
              f"{ref} - please compare SI and carrier draft BL",
              "Shipping instruction (XML export), invoice and the carrier's "
              "draft BL. Please compare the SI against the BL and confirm.",
              ["SHP-004_SI.xml", "SHP-004_BL.txt",
               "SHP-004_Invoice.docx"], 85)

    # ---------------------------------------------------------------- SHP-005
    # One ZIP from the forwarder + a follow-up email from a second sender.
    ref = "SHP-005"
    b5 = dict(base, weight="18,650 KG", containers="1 x 40'HC",
              booking="MSDU100000005", container_no="ABCU4440009",
              vessel="NYK VENUS", voyage="221E",
              pol="PORT KLANG, MALAYSIA (MYPKG)",
              pod="VALENCIA, SPAIN (ESVLC)",
              shipper="GREEN HARVEST FOODS SDN BHD",
              consignee="VALENCIA FOODS SL",
              notify="VALENCIA FOODS SL",
              goods="340 CARTONS CANNED FOODSTUFFS")
    zip_payload = _zip([
        ("Shipping_Instruction.pdf", _render_pdf([_si_doc(ref, **b5)])),
        ("Bill_of_Lading.pdf",
         _render_pdf([_bl_doc(ref, **b5, bl_no="MEDUUD100005")])),
        ("Commercial_Invoice.xlsx", _xlsx([
            ("COMMERCIAL INVOICE", ""),
            ("Invoice No.:", "INV-2026-0005"),
            ("Shipment Ref:", ref),
            ("Shipper:", b5["shipper"]),
            ("Consignee:", b5["consignee"]),
            ("Total Amount:", "USD 71,300.00"),
        ])),
    ])
    attachments["SHP-005_shipment_documents.zip"] = zip_payload
    attachments["SHP-005_Invoice_rev2.jpeg"] = _render_image(
        _render_pdf([_invoice_doc(ref, number="INV-2026-0005-R2",
                                  total="72,050.00",
                                  goods="340 CARTONS CANNED FOODSTUFFS")]),
        ".jpeg", dpi=200)
    add_email(7, ref, "forwarder",
              f"{ref} - please check the attached documents (bundle)",
              "All documents in one archive: shipping instruction, bill of "
              "lading and commercial invoice. Please check and confirm.",
              ["SHP-005_shipment_documents.zip"], 100)
    add_email(8, ref, "accounts",
              f"{ref} - revised commercial invoice (scan)",
              "Our accounts team reissued the invoice; please use the attached "
              "revised copy instead of the one in the bundle.",
              ["SHP-005_Invoice_rev2.jpeg"], 115)

    # ---------------------------------------------------------------- SHP-006
    # Scanned bill of lading: a TIFF with no embedded text, so OCR is the only
    # way in and the result is allowed to be imperfect.
    ref = "SHP-006"
    b6 = dict(base, weight="27,400 KG", containers="2 x 20'GP",
              booking="MSDU100000006", container_no="ABCU2220014",
              vessel="MAERSK SENTOSA", voyage="204W",
              pol="PORT KLANG, MALAYSIA (MYPKG)",
              pod="GDANSK, POLAND (PLGDN)",
              shipper="BORNEO TIMBER PRODUCTS SDN BHD",
              consignee="GDANSK TIMBER SP ZOO",
              notify="GDANSK TIMBER SP ZOO",
              goods="26 PACKETS SAWN TIMBER")
    attachments["SHP-006_SI.txt"] = _si_doc(ref, **b6)
    # Rendered at a deliberately low resolution - a real fax-quality scan, which
    # is exactly the case where OCR has to be honest about being unsure.
    bl6 = _render_pdf([_bl_doc(ref, **b6, bl_no="MEDUUD100006")])
    attachments["SHP-006_BL.tiff"] = _render_image(bl6, ".tiff", dpi=140)
    attachments["SHP-006_BL_scan.pdf"] = bl6
    add_email(9, ref, "agent",
              f"{ref} - please verify SI against scanned bill of lading",
              "Shipping instruction attached. The bill of lading is a poor "
              "quality scan from the port agent - please verify carefully "
              "because the values may be hard to read.",
              ["SHP-006_SI.txt", "SHP-006_BL.tiff"], 130)

    # --------------------------------------------------------------- write out
    out_dir.mkdir(parents=True, exist_ok=True)
    att_dir = out_dir / "attachments"
    att_dir.mkdir(exist_ok=True)
    for name, data in attachments.items():
        # .txt documents are authored as text here and stored as bytes, exactly
        # like an attachment arriving over HTTP.
        (att_dir / name).write_bytes(
            data.encode("utf-8") if isinstance(data, str) else data)

    (out_dir / "emails.json").write_text(
        json.dumps({"mailbox": TO, "note": "curated demo shipments",
                    "emails": emails}, indent=2, ensure_ascii=False),
        encoding="utf-8")

    expectations = {
        "SHP-001": {"expect": "VERIFIED", "complete": True,
                    "note": "all fields agree; BL fields live on PDF page 2"},
        "SHP-002": {"expect": "missing_BL", "missing": ["BL"],
                    "note": "no BL is ever sent"},
        "SHP-003": {"expect": "version_control",
                    "si_versions_at_least": 3, "current_si_weight": "41500",
                    "note": "v1(38900) -> v2(40150) -> v3(41500); BL matches v3"},
        "SHP-004": {"expect": "mismatch", "mismatch_fields": ["container_count"],
                    "note": "SI 3 containers vs BL 4 containers"},
        "SHP-005": {"expect": "grouped", "source_emails_at_least": 2,
                    "note": "zip from forwarder + scan from accounts"},
        "SHP-006": {"expect": "ocr", "note": "scanned TIFF BL read by OCR"},
    }
    (out_dir / "expectations.json").write_text(
        json.dumps(expectations, indent=2), encoding="utf-8")

    return {"emails": len(emails), "attachments": len(attachments)}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", default=str(BACKEND_ROOT / "demo-shipments"))
    args = ap.parse_args()
    stats = build(Path(args.out))
    print(f"wrote {stats['emails']} emails and "
          f"{stats['attachments']} attachments to {args.out}")


if __name__ == "__main__":
    main()
