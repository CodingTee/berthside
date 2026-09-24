"""Acceptance tests A–G for the BerthSide P0/P1 fixes.

Drives the **real** Gmail sync code path with only the Gmail API stubbed, in an
isolated database and ingest directory. Classification, extraction, versioning
and verification are the production code.

    A  SI v1/v2/v3  -> ONE shipment, v1/v2/v3 with v3 CURRENT, in BOTH Gmail
       return orders (newest-first and oldest-first)
    A2 a late-arriving OLDER email must be inserted as history, not become current
    B  a real attachment must beat the synthetic body document; realistic
       filenames must be recognised
    C  missing BL -> NEEDS_ATTENTION, no fake BL row
    D  field mismatch -> NEEDS_ATTENTION with the field retrievable
    E  GET /api/emails/{id} is read-only
    F  repeated Dashboard reads change nothing
    G  Gmail Date header -> EmailRecord.received_at (with timezone handling)

Usage:
    python scripts/audit_gmail_versions.py
"""
from __future__ import annotations

import base64
import os
import sys
import tempfile
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND_ROOT))

_TMP = Path(tempfile.mkdtemp(prefix="berthside-accept-"))
_DB_URL = f"sqlite:///{(_TMP / 'accept.db').as_posix()}"
os.environ["DATABASE_URL"] = _DB_URL
os.environ["DATABASE_URL_ENTERPRISE"] = _DB_URL
os.environ["DATABASE_URL_OAUTH"] = _DB_URL
os.environ["INGEST_DIR"] = str(_TMP / "ingested")
os.environ["DATA_SOURCE"] = str(_TMP / "empty-bundle")
os.environ.setdefault("OCR_ENABLED", "0")

from app.database import SessionLocal, init_db  # noqa: E402
from app.integrations.gmail import client, parser, sync  # noqa: E402
from app.models import (DocumentVersionRecord, EmailRecord,  # noqa: E402
                        ReportRecord, ShipmentRecord)
from app.services import shipment_overview  # noqa: E402

PASS: list[str] = []
FAIL: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    (PASS if ok else FAIL).append(name)
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))
    return ok


# --------------------------------------------------------------------------- #
# fixtures                                                                    #
# --------------------------------------------------------------------------- #
SI_WEIGHTS = {"v0": "35,000 KG", "v1": "38,900 KG", "v2": "40,150 KG",
              "v3": "41,500 KG"}
SI_DATES = {
    "v1": "Fri, 18 Sep 2026 09:00:00 +0800",
    "v2": "Sat, 19 Sep 2026 09:00:00 +0800",
    "v3": "Sun, 20 Sep 2026 09:00:00 +0800",
}
SI_SUBJECT = {
    "v1": "Shipping Instruction - SHP-001 - ABC Logistics",
    "v2": "Updated Shipping Instruction - SHP-001 - ABC Logistics",
    "v3": "Final Shipping Instruction - SHP-001 - ABC Logistics",
}


def si_text(version: str, containers: str = "2 x 40'HC", ref: str = "SHP-001") -> str:
    return f"""SHIPPING INSTRUCTION
========================================

Shipper/Exporter: ABC LOGISTICS SDN BHD
CONSIGNEE: ROTTERDAM TRADING BV
NOTIFY PARTY: ROTTERDAM TRADING BV
Port of Loading: PORT KLANG, MALAYSIA (MYPKG)
Discharge Port: ROTTERDAM, NETHERLANDS (NLRTM)
No. of Containers or Packages: {containers}
Gross Weight (KG): {SI_WEIGHTS[version]}
Vessel Name: EVER GLORY
Voy. No: 026E
Container No.: ABCU1234567
Shipment ID: {ref}
OC No.: {ref}
Freight: PREPAID
"""


def bl_text(containers: str = "2 x 40'HC", weight: str = "41,500 KG",
            ref: str = "SHP-001") -> str:
    return f"""BILL OF LADING (DRAFT)
========================================

SHIPPER: ABC LOGISTICS SDN BHD
CONSIGNEE: ROTTERDAM TRADING BV
Notify: ROTTERDAM TRADING BV
Port of Loading (POL): PORT KLANG, MALAYSIA (MYPKG)
POD: ROTTERDAM, NETHERLANDS (NLRTM)
Container Count: {containers}
Gross Wt (kgs): {weight}
Vessel Name: EVER GLORY
Voyage: 026E
Commodity: AUTOMOTIVE SPARE PARTS
Bill of Lading No.: MEDUUD100001
Shipment ID: {ref}
OC No.: {ref}
Freight: PREPAID
"""


def invoice_text(ref: str = "SHP-001") -> str:
    return f"""COMMERCIAL INVOICE
========================================

Invoice No.: INV-2026-0001
Shipment Ref: {ref}
Total Amount: USD 182,400.00
"""


def pdf(text: str) -> bytes:
    import fitz

    doc = fitz.open()
    page = doc.new_page()
    y = 60
    for line in text.splitlines():
        page.insert_text((60, y), line, fontsize=9.5, fontname="cour")
        y += 15
    data = doc.tobytes(deflate=True)
    doc.close()
    return data


_BLOBS: dict[str, bytes] = {}


def message(msg_id: str, subject: str, sender: str, body: str,
            attachment_name: str | None, attachment: bytes | None,
            date_header: str | None = None,
            internal_date: int | None = None) -> dict:
    parts = [{"mimeType": "text/plain",
              "body": {"data": base64.urlsafe_b64encode(body.encode()).decode().rstrip("=")}}]
    if attachment_name:
        _BLOBS[f"att-{msg_id}"] = attachment
        parts.append({"mimeType": "application/pdf", "filename": attachment_name,
                      "body": {"attachmentId": f"att-{msg_id}", "size": len(attachment)}})
    headers = [{"name": "Subject", "value": subject},
               {"name": "From", "value": sender}]
    if date_header:
        headers.append({"name": "Date", "value": date_header})
    payload: dict = {"mimeType": "multipart/mixed", "headers": headers, "parts": parts}
    raw = {"id": msg_id, "threadId": f"thread-{msg_id}", "historyId": "1",
           "payload": payload}
    if internal_date is not None:
        raw["internalDate"] = str(internal_date)
    return raw


def stub(messages: dict[str, dict], order: list[str]) -> None:
    client.list_message_ids = lambda max_results=None, query=None: list(order)
    client.get_message = lambda message_id: messages[message_id]
    client.get_attachment = lambda message_id, attachment_id: _BLOBS[attachment_id]


def si_messages(naming: str, ref: str = "SHP-001", tag: str = "si",
                with_body_doc: bool = False) -> dict[str, dict]:
    """Three SI emails for one shipment.

    ``naming`` picks the filename style the sender used. The returned dict is
    keyed by Gmail message id, which is what the stubbed client looks messages
    up by.
    """
    def name(version: str) -> str:
        if naming == "convention":
            return "SHP-001_SI.pdf"
        return {
            "plain": f"SI_{version}.pdf",
            "descriptive": "Shipping_Instruction_SHP-001.pdf",
            "mixed": f"SHP-001_SI_{version.upper()}.pdf",
        }[naming]

    out: dict[str, dict] = {}
    for version in ("v1", "v2", "v3"):
        # The body names the document inline. With a real attachment present the
        # body must be ignored entirely — that is acceptance test B.
        body = (f"Dear Shipping Team,\n\nPlease find attached the shipping "
                f"instruction for shipment {ref} (copy {version}).\n\n"
                f"Best regards,\nABC Logistics")
        if with_body_doc:
            body += "\n\n" + si_text(version, ref=ref)
        msg_id = f"{tag}-{version}"
        out[msg_id] = message(
            msg_id, SI_SUBJECT[version].replace("SHP-001", ref),
            "Amelia Tan <amelia.tan@abclogistics.com>", body,
            name(version), pdf(si_text(version, ref=ref)), SI_DATES[version])
    return out


def load(order: list[str], messages: dict[str, dict]) -> dict:
    stub(messages, order)
    db = SessionLocal()
    try:
        return sync.poll_and_process(db)
    finally:
        db.close()


def si_versions(shipment_id: int) -> list[DocumentVersionRecord]:
    db = SessionLocal()
    try:
        return (db.query(DocumentVersionRecord)
                .filter_by(shipment_id=shipment_id, doc_type="SI",
                           document_status="ACTIVE")
                .order_by(DocumentVersionRecord.version_number).all())
    finally:
        db.close()


def current_si(shipment_id: int) -> DocumentVersionRecord | None:
    db = SessionLocal()
    try:
        return (db.query(DocumentVersionRecord)
                .filter_by(shipment_id=shipment_id, doc_type="SI", is_latest=1)
                .first())
    finally:
        db.close()


# --------------------------------------------------------------------------- #
# tests                                                                       #
# --------------------------------------------------------------------------- #
def test_a(ref: str, tag: str, naming: str, order_name: str,
           order: list[str]) -> int | None:
    print(f"\nTEST A  three SI versions, ref={ref}, filenames={naming}, "
          f"Gmail order={order_name}")
    messages = si_messages(naming, ref=ref, tag=tag)
    load([f"{tag}-{v}" for v in order], messages)
    db = SessionLocal()
    shipments = db.query(ShipmentRecord).filter(
        ShipmentRecord.shipment_key == f"REF:{ref}").all()
    emails = db.query(EmailRecord).filter(
        EmailRecord.email_id.like(f"GMAIL-{tag}-%")).count()
    db.close()

    check("exactly one shipment", len(shipments) == 1,
          f"shipments={[s.shipment_key for s in shipments]}")
    if not shipments:
        return None
    sid = shipments[0].id
    versions = si_versions(sid)
    check("three SI versions preserved", len(versions) == 3,
          f"count={len(versions)}")
    weights = [v.extracted_fields.get("gross_weight_kg") for v in versions]
    check("numbered oldest -> newest", weights == [38900.0, 40150.0, 41500.0],
          f"weights={weights}")
    current = current_si(sid)
    check("v3 is CURRENT", bool(current) and
          current.extracted_fields.get("gross_weight_kg") == 41500.0,
          f"current weight={current.extracted_fields.get('gross_weight_kg') if current else None}")
    check("three source emails kept separate", emails == 3, f"emails={emails}")
    check("every version traces to its own email",
          len({v.email_id for v in versions}) == 3,
          f"email_ids={[v.email_id for v in versions]}")
    return sid


def test_a2(sid: int, ref: str) -> None:
    print(f"\nTEST A2  a late-arriving OLDER email must not take over as current ({ref})")
    before = current_si(sid)
    older = message("older-v0", f"Shipping Instruction (earlier draft) - {ref}",
                    "Amelia Tan <amelia.tan@abclogistics.com>",
                    f"Attached is the earlier shipping instruction for {ref}.",
                    "SI_v0.pdf",
                    pdf(si_text("v0", containers="1 x 20'GP", ref=ref)),
                    "Thu, 17 Sep 2026 09:00:00 +0800")
    load(["older-v0"], {"older-v0": older})
    versions = si_versions(sid)
    after = current_si(sid)
    check("older document preserved as a version", len(versions) == 4,
          f"count={len(versions)}")
    check("current version unchanged (still the newest document)",
          after is not None and after.id == before.id,
          f"current weight={after.extracted_fields.get('gross_weight_kg') if after else None}")
    check("it was inserted as the earliest version",
          versions[0].filename.endswith("SI_v0.pdf")
          and versions[0].extracted_fields.get("gross_weight_kg") == 35000.0,
          f"v1 file={versions[0].filename.rsplit('/', 1)[-1]} "
          f"weight={versions[0].extracted_fields.get('gross_weight_kg')}")


def test_b() -> None:
    print("\nTEST B  a real attachment must beat the synthetic body document")
    ref = "SHP-101"
    # These bodies also contain the whole SI text, so a synthetic document is
    # possible; the real PDF must still win.
    messages = si_messages("plain", ref=ref, tag="b", with_body_doc=True)
    load(["b-v3", "b-v2", "b-v1"], messages)

    db = SessionLocal()
    shipment = db.query(ShipmentRecord).filter_by(shipment_key=f"REF:{ref}").first()
    if shipment:
        versions = (db.query(DocumentVersionRecord)
                    .filter_by(shipment_id=shipment.id, doc_type="SI",
                               document_status="ACTIVE")
                    .order_by(DocumentVersionRecord.version_number).all())
    else:
        versions = []
    db.close()

    check("shipment created from the real attachments", shipment is not None)
    if not shipment:
        return
    check("the real PDFs were read, not the body",
          [v.extracted_fields.get("gross_weight_kg") for v in versions]
          == [38900.0, 40150.0, 41500.0],
          f"weights={[v.extracted_fields.get('gross_weight_kg') for v in versions]}")
    check("no version was built from the email body",
          all("frombody" not in v.filename for v in versions),
          f"files={[v.filename.rsplit('/', 1)[-1] for v in versions]}")
    check("the sender's own filenames are what was stored",
          all(v.filename.endswith(".pdf") for v in versions),
          f"files={[v.filename.rsplit('/', 1)[-1] for v in versions]}")

    # Every realistic naming style must resolve to SI.
    from app.services import doc_types
    for name in ("SI_v1.pdf", "SI_v2.pdf", "SI_v3.pdf",
                 "Shipping_Instruction_SHP-001.pdf", "SHP-001_SI_Final.pdf"):
        check(f"{name} detected as SI", doc_types.detect(name) == "SI",
              f"got {doc_types.detect(name)}")
    check("an ambiguous name is still refused",
          doc_types.detect("SI_and_BL.pdf") is None)


def test_c() -> None:
    print("\nTEST C  missing BL")
    ref = "SHP-002"
    messages = {
        "c-si": message("c-si", f"Shipping Instruction - {ref} - please verify",
                        "Docs <export.docs@abclogistics.com>",
                        f"Attached is the shipping instruction for {ref}. "
                        "Please check what we have so far.",
                        f"{ref}_SI.pdf", pdf(si_text("v1", ref=ref)),
                        "Sat, 19 Sep 2026 10:00:00 +0800"),
        "c-inv": message("c-inv", f"Commercial Invoice - {ref}",
                         "Invoicing <ar@abclogistics.com>",
                         f"Attached is the commercial invoice for shipment {ref}.",
                         f"{ref}_Invoice.pdf", pdf(invoice_text(ref)),
                         "Sat, 19 Sep 2026 11:00:00 +0800"),
    }
    load(["c-inv", "c-si"], messages)
    db = SessionLocal()
    shipment = db.query(ShipmentRecord).filter_by(shipment_key=f"REF:{ref}").first()
    overview = shipment_overview.build_overview(db, shipment) if shipment else {}
    db.close()
    check("shipment exists", shipment is not None)
    if not shipment:
        return
    check("status is NEEDS_ATTENTION", overview["status"] == "NEEDS_ATTENTION",
          f"status={overview['status']}")
    check("missing document is the BL", overview["missing_documents"] == ["BL"],
          f"missing={overview['missing_documents']}")
    check("reported as a shipment reason, not a document type",
          any("Bill of Lading" in r for r in overview["reasons"]),
          f"reasons={overview['reasons']}")
    check("no fake BL document created",
          overview["documents"]["BL"]["present"] is False
          and overview["documents"]["BL"]["version_count"] == 0)
    check("invoice still recognised", overview["documents"]["INVOICE"]["present"])
    check("Request BL offered", "REQUEST_BL" in overview["actions"],
          f"actions={overview['actions']}")


def test_d() -> None:
    print("\nTEST D  field mismatch")
    ref = "SHP-003"
    messages = {
        "d-si": message("d-si", f"Shipping Instruction - {ref} - please compare",
                        "Docs <export.docs@abclogistics.com>",
                        "Shipping instruction and draft BL attached. Please compare "
                        "the SI against the BL and confirm.",
                        f"{ref}_SI.pdf",
                        pdf(si_text("v3", containers="3 x 40'HC", ref=ref)),
                        "Sun, 20 Sep 2026 08:00:00 +0800"),
        "d-bl": message("d-bl", f"Bill of Lading - {ref}",
                        "Ops <ops.desk@globalshippingline.com>",
                        f"Attached is the bill of lading for {ref}.",
                        f"{ref}_BL.pdf",
                        pdf(bl_text(containers="4 x 40'HC", ref=ref)),
                        "Sun, 20 Sep 2026 09:00:00 +0800"),
    }
    load(["d-bl", "d-si"], messages)
    db = SessionLocal()
    shipment = db.query(ShipmentRecord).filter_by(shipment_key=f"REF:{ref}").first()
    overview = shipment_overview.build_overview(db, shipment) if shipment else {}
    db.close()
    check("shipment exists", shipment is not None)
    if not shipment:
        return
    check("status is NEEDS_ATTENTION", overview["status"] == "NEEDS_ATTENTION",
          f"status={overview['status']}")
    check("mismatch field is retrievable",
          "container_count" in overview["mismatch_fields"],
          f"mismatch={overview['mismatch_fields']}")
    issue = overview["issues"][0] if overview["issues"] else {}
    check("both values stored for the reviewer",
          issue.get("si_value") == 3 and issue.get("bl_value") == 4,
          f"si={issue.get('si_value')} bl={issue.get('bl_value')}")


def test_e() -> None:
    print("\nTEST E  GET /api/emails/{id} must be read-only")
    from app.routers import frontend_compat

    db = SessionLocal()
    try:
        # An email that exists in the inbox but has never been processed.
        email = EmailRecord(email_id="UNPROC-1", sender="a@b.com",
                            subject="Weekly berthing report",
                            body="Nothing to do here.", attachments=[])
        db.add(email)
        db.commit()
        before_reports = db.query(ReportRecord).count()
        first = frontend_compat.email_detail("UNPROC-1", db=db)
        for _ in range(5):
            frontend_compat.email_detail("UNPROC-1", db=db)
        after_reports = db.query(ReportRecord).count()
        row = db.query(ReportRecord).filter_by(email_id="UNPROC-1").first()
    finally:
        db.close()
    check("no report created by reading", after_reports == before_reports,
          f"{before_reports} -> {after_reports}")
    check("no report row for the email", row is None)
    check("response is still complete",
          first.get("processed") is False and first.get("result") is None
          and first["email"]["email_id"] == "UNPROC-1")


def test_f() -> None:
    print("\nTEST F  repeated dashboard reads change nothing")
    db = SessionLocal()
    try:
        def snapshot():
            return (
                db.query(DocumentVersionRecord).count(),
                db.query(ShipmentRecord).count(),
                db.query(ReportRecord).count(),
                tuple(sorted((r.email_id, r.attempts) for r in
                             db.query(ReportRecord).all())),
                tuple(sorted((v.id, v.version_number, v.is_latest) for v in
                             db.query(DocumentVersionRecord).all())),
            )

        before = snapshot()
        for _ in range(10):
            for shipment in db.query(ShipmentRecord).all():
                shipment_overview.build_overview(db, shipment)
        after = snapshot()
    finally:
        db.close()
    check("no new documents or versions", before[0] == after[0],
          f"versions {before[0]} -> {after[0]}")
    check("no new shipments", before[1] == after[1])
    check("no new reports", before[2] == after[2])
    check("processing attempts unchanged", before[3] == after[3])
    check("current versions unchanged", before[4] == after[4])


def test_g() -> None:
    print("\nTEST G  Gmail Date header -> received_at")
    db = SessionLocal()
    try:
        rows = {e.email_id: e.received_at for e in db.query(EmailRecord).all()}
        versions = {v.email_id: v.received_at for v in
                    db.query(DocumentVersionRecord).all()}
    finally:
        db.close()
    # "Fri, 18 Sep 2026 09:00:00 +0800" is 2026-09-18T01:00:00Z. Gmail stores the
    # message as GMAIL-<id>, hence the prefix.
    v1 = versions.get("GMAIL-si-v1")
    check("Date header parsed and shifted to UTC",
          v1 is not None and v1.isoformat().startswith("2026-09-18T01:00:00"),
          f"received_at={v1}")
    check("every parsed email has a timestamp",
          all(rows.get(f"GMAIL-si-{v}") is not None for v in ("v1", "v2", "v3")),
          f"received_at={[str(rows.get(f'GMAIL-si-{v}')) for v in ('v1', 'v2', 'v3')]}")
    check("versions carry the same timestamp as their email",
          all(versions.get(f"GMAIL-si-{v}") == rows.get(f"GMAIL-si-{v}")
              for v in ("v1", "v2", "v3")))

    # Malformed and missing headers must not stop ingestion.
    from app.integrations.gmail.parser import _received_iso
    check("malformed Date tolerated",
          _received_iso({}, {"date": "not a date"}) is None)
    check("missing Date tolerated", _received_iso({}, {}) is None)
    check("internalDate used as fallback",
          (_received_iso({"internalDate": 1758234000000}, {}) or "").startswith("2025-09-18"))
    check("timezone offset applied",
          _received_iso({}, {"date": "Fri, 19 Sep 2026 23:30:00 -0700"})
          == "2026-09-20T06:30:00+00:00",
          _received_iso({}, {"date": "Fri, 19 Sep 2026 23:30:00 -0700"}))


def main() -> int:
    init_db()
    # Both Gmail return orders, and both filename styles.
    test_a("SHP-001", "si", "plain", "newest-first", ["v3", "v2", "v1"])
    sid = test_a("SHP-011", "conv", "convention", "oldest-first", ["v1", "v2", "v3"])
    if sid:
        test_a2(sid, "SHP-011")
    test_b()
    test_c()
    test_d()
    test_e()
    test_f()
    test_g()

    print("\n" + "=" * 70)
    print(f"{len(PASS)} passed, {len(FAIL)} failed")
    for name in FAIL:
        print(f"  FAILED: {name}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
