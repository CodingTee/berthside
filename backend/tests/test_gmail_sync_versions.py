"""Focused regression test: three Gmail emails -> three SI versions -> one shipment.

Drives the **real** `app.integrations.gmail.sync` code path with only the Gmail
API stubbed (`client.list_message_ids` / `get_message` / `get_attachment`), so
classification, attachment handling, extraction, versioning, verification and
shipment grouping are all production code.

Covers the acceptance criteria:

    three separate Gmail emails       -> three EmailRecords, three GmailMessageRecords
    each message classified           -> each report is BL_COMPARISON
    documents extracted from the file -> weights come from the attachments
    grouped into ONE shipment         -> no duplicate shipments
    SI v1/v2/v3 preserved             -> all three ACTIVE versions retrievable
    v3 is current                     -> is_latest on the newest document only
    v1/v2 not overwritten             -> their rows and content are unchanged
    attachment/document metadata kept -> filename + source email per version
    expected shipment state           -> build_overview
    missing BL stays incomplete       -> complete=False, NEEDS_ATTENTION, no BL row
    GET requests do not mutate state  -> reports/versions/shipments/attempts unchanged

Gmail returns messages newest-first in reality; every version test runs in that
order so the assertion is meaningful.
"""
from __future__ import annotations

import base64

import pytest

from app.integrations.gmail import client as gmail_client
from app.integrations.gmail import sync
from app.models import (DocumentVersionRecord, EmailRecord, GmailMessageRecord,
                        ReportRecord, ShipmentRecord)
from app.services import shipment_overview

# Distinct weights identify which document a version came from. The bodies carry
# no field data at all, so a version with a weight can only have come from the
# attachment — that is what proves extraction used the file, not the email text.
SI_WEIGHTS = {"v1": "38,900 KG", "v2": "40,150 KG", "v3": "41,500 KG"}
SI_DATES = {
    "v1": "Fri, 18 Sep 2026 09:00:00 +0800",
    "v2": "Sat, 19 Sep 2026 09:00:00 +0800",
    "v3": "Sun, 20 Sep 2026 09:00:00 +0800",
}
SI_SUBJECTS = {
    "v1": "Shipping Instruction - SHP-001 - ABC Logistics",
    "v2": "Updated Shipping Instruction - SHP-001 - ABC Logistics",
    "v3": "Final Shipping Instruction - SHP-001 - ABC Logistics",
}


def si_text(version: str, ref: str = "SHP-001") -> str:
    return f"""SHIPPING INSTRUCTION
========================================

Shipper/Exporter: ABC LOGISTICS SDN BHD
CONSIGNEE: ROTTERDAM TRADING BV
NOTIFY PARTY: ROTTERDAM TRADING BV
Port of Loading: PORT KLANG, MALAYSIA (MYPKG)
Discharge Port: ROTTERDAM, NETHERLANDS (NLRTM)
No. of Containers or Packages: 2 x 40'HC
Gross Weight (KG): {SI_WEIGHTS[version]}
Vessel Name: EVER GLORY
Voy. No: 026E
Container No.: ABCU1234567
Shipment ID: {ref}
OC No.: {ref}
Freight: PREPAID
"""


def bl_text(ref: str = "SHP-001") -> str:
    return f"""BILL OF LADING (DRAFT)
========================================

SHIPPER: ABC LOGISTICS SDN BHD
CONSIGNEE: ROTTERDAM TRADING BV
Notify: ROTTERDAM TRADING BV
Port of Loading (POL): PORT KLANG, MALAYSIA (MYPKG)
POD: ROTTERDAM, NETHERLANDS (NLRTM)
Container Count: 2 x 40'HC
Gross Wt (kgs): 41,500 KG
Bill of Lading No.: MEDUUD100001
Shipment ID: {ref}
OC No.: {ref}
Freight: PREPAID
"""


def _b64(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode()).decode("ascii")


def gmail_message(msg_id: str, subject: str, attachment: str,
                  date_header: str | None = None,
                  internal_date: int | None = None,
                  body: str = "Dear Shipping Team,\n\n"
                              "Please check the attached document and confirm."
                              "\n\nBest regards,\nABC Logistics") -> dict:
    """A Gmail API message shape: one text/plain part + one attachment."""
    headers = [
        {"name": "From", "value": "Amelia Tan <amelia.tan@abclogistics.com>"},
        {"name": "Subject", "value": subject},
    ]
    if date_header is not None:
        headers.append({"name": "Date", "value": date_header})
    message = {
        "id": msg_id,
        "threadId": f"thread-{msg_id}",
        "historyId": "1",
        "payload": {
            "mimeType": "multipart/mixed",
            "headers": headers,
            "parts": [
                {"mimeType": "text/plain", "body": {"data": _b64(body)}},
                {"filename": attachment,
                 "body": {"attachmentId": f"att-{msg_id}"}},
            ],
        },
    }
    if internal_date is not None:
        message["internalDate"] = str(internal_date)
    return message


def stub_gmail(monkeypatch, messages: dict[str, dict], blobs: dict[str, bytes],
               order: list[str]) -> None:
    """Stub only the Gmail API. Everything downstream is production code."""
    monkeypatch.setattr(gmail_client, "list_message_ids",
                        lambda max_results=None, query=None: list(order))
    monkeypatch.setattr(gmail_client, "get_message",
                        lambda message_id: messages[message_id])
    monkeypatch.setattr(gmail_client, "get_attachment",
                        lambda message_id, attachment_id: blobs[attachment_id])


def three_si_messages(ref: str = "SHP-001", tag: str = "si") -> tuple[dict, dict]:
    messages: dict[str, dict] = {}
    blobs: dict[str, bytes] = {}
    for version in ("v1", "v2", "v3"):
        msg_id = f"{tag}-{version}"
        messages[msg_id] = gmail_message(
            msg_id, SI_SUBJECTS[version].replace("SHP-001", ref),
            f"{ref}_SI_{version}.txt", SI_DATES[version])
        blobs[f"att-{msg_id}"] = si_text(version, ref=ref).encode()
    return messages, blobs


def _versions(db, shipment_id: int, doc_type: str = "SI") -> list[DocumentVersionRecord]:
    return (db.query(DocumentVersionRecord)
            .filter_by(shipment_id=shipment_id, doc_type=doc_type)
            .order_by(DocumentVersionRecord.version_number).all())


# --------------------------------------------------------------------------- #
def test_three_gmail_emails_become_one_shipment_with_three_si_versions(
        monkeypatch, db_session):
    """The headline acceptance test, in Gmail's real newest-first order."""
    messages, blobs = three_si_messages()
    stub_gmail(monkeypatch, messages, blobs, ["si-v3", "si-v2", "si-v1"])

    result = sync.poll_and_process(db_session)
    assert result["failed"] == 0, result
    assert result["processed"] == 3

    # --- three separate emails, each one classified ------------------------
    emails = db_session.query(EmailRecord).order_by(EmailRecord.email_id).all()
    assert [e.email_id for e in emails] == [
        "GMAIL-si-v1", "GMAIL-si-v2", "GMAIL-si-v3"]
    gmail_rows = (db_session.query(GmailMessageRecord)
                  .order_by(GmailMessageRecord.gmail_message_id).all())
    assert [r.gmail_message_id for r in gmail_rows] == ["si-v1", "si-v2", "si-v3"]
    for row in gmail_rows:
        assert row.processing_status == "PROCESSED"
        assert row.thread_id == f"thread-{row.gmail_message_id}"
    reports = {r.email_id: r for r in db_session.query(ReportRecord).all()}
    assert set(reports) == {"GMAIL-si-v1", "GMAIL-si-v2", "GMAIL-si-v3"}
    for report in reports.values():
        # "please check ... and confirm" is the intent the classifier reads as a
        # comparison request.
        assert report.category == "BL_COMPARISON"
        # SI is present, BL is not: the honest verdict is needs-review/missing.
        assert report.status == "NEEDS_REVIEW"
        assert report.review_reason == "missing_attachment"

    # --- ONE shipment, and no duplicates ----------------------------------
    shipments = db_session.query(ShipmentRecord).all()
    assert len(shipments) == 1
    shipment = shipments[0]
    assert shipment.shipment_key == "REF:SHP-001"
    assert shipment.reference_number == "SHP-001"

    # --- three SI versions, all preserved, v3 current ---------------------
    versions = _versions(db_session, shipment.id)
    assert len(versions) == 3
    assert [v.version_number for v in versions] == [1, 2, 3]
    assert [v.extracted_fields["gross_weight_kg"] for v in versions] == [
        38900.0, 40150.0, 41500.0]
    assert [v.document_status for v in versions] == ["ACTIVE"] * 3

    current = [v for v in versions if v.is_latest]
    assert len(current) == 1, "exactly one SI version may be current"
    assert current[0].version_number == 3
    assert current[0].extracted_fields["gross_weight_kg"] == 41500.0

    # --- v1/v2 were not overwritten, and are retrievable ------------------
    assert versions[0].extracted_fields["gross_weight_kg"] == 38900.0
    assert versions[1].extracted_fields["gross_weight_kg"] == 40150.0
    assert versions[0].duplicate_of_version_id is None
    assert versions[1].duplicate_of_version_id is None
    # previous_version_id chains them forwards rather than replacing them.
    assert versions[0].previous_version_id is None
    assert versions[1].previous_version_id == versions[0].id
    assert versions[2].previous_version_id == versions[1].id
    assert versions[0].id not in (versions[1].id, versions[2].id)

    # --- document/attachment metadata survives ---------------------------
    assert [v.filename.rsplit("/", 1)[-1] for v in versions] == [
        "SHP-001_SI_v1.txt", "SHP-001_SI_v2.txt", "SHP-001_SI_v3.txt"]
    assert [v.email_id for v in versions] == [
        "GMAIL-si-v1", "GMAIL-si-v2", "GMAIL-si-v3"]
    assert all(v.received_at is not None for v in versions)
    # ...and the extracted values really came from the attachments: the bodies
    # contain no field data, so a body-derived document would extract nothing.
    assert all(v.extracted_fields.get("shipper") == "ABC LOGISTICS SDN BHD"
               for v in versions)

    # --- expected shipment state ------------------------------------------
    overview = shipment_overview.build_overview(db_session, shipment)
    assert overview["shipment_id"] == "SHP-001"
    assert overview["documents"]["SI"]["present"] is True
    assert overview["documents"]["SI"]["version_count"] == 3
    assert overview["documents"]["SI"]["current_version_number"] == 3
    assert overview["documents"]["BL"]["present"] is False
    assert overview["complete"] is False
    assert overview["missing_documents"] == ["BL"]
    assert overview["status"] == "NEEDS_ATTENTION"

    # --- a second poll must not duplicate anything ------------------------
    again = sync.poll_and_process(db_session)
    assert again["processed"] == 0
    assert again["skipped"] == 3
    assert db_session.query(ShipmentRecord).count() == 1
    assert len(_versions(db_session, shipment.id)) == 3


def test_versions_are_chronological_even_from_oldest_first(monkeypatch, db_session):
    """The mirror image of the test above: order must not decide anything."""
    messages, blobs = three_si_messages(ref="SHP-011", tag="conv")
    stub_gmail(monkeypatch, messages, blobs, ["conv-v1", "conv-v2", "conv-v3"])

    sync.poll_and_process(db_session)

    shipment = db_session.query(ShipmentRecord).one()
    versions = _versions(db_session, shipment.id)
    assert [v.extracted_fields["gross_weight_kg"] for v in versions] == [
        38900.0, 40150.0, 41500.0]
    assert [v.is_latest for v in versions] == [0, 0, 1]


def test_grouping_does_not_depend_on_the_classified_intent(monkeypatch, db_session):
    """An SI-only mail is filed whether it reads as a request or a comparison.

    The classifier distinguishes *why* an email was sent, not whether a shipment
    exists, so both document-carrying categories must produce the same shipment.
    """
    msg_id = "si-request"
    plain_body = ("Dear Shipping Team,\n\nShipping instruction for this shipment "
                  "is attached.\n\nBest regards,\nABC Logistics")
    stub_gmail(
        monkeypatch,
        {msg_id: gmail_message(msg_id, "Shipping Instruction - SHP-021",
                               "SHP-021_SI.txt",
                               "Sat, 19 Sep 2026 09:00:00 +0800",
                               body=plain_body)},
        {f"att-{msg_id}": si_text("v1", ref="SHP-021").encode()},
        [msg_id])
    sync.poll_and_process(db_session)

    report = db_session.query(ReportRecord).one()
    assert report.email_id == "GMAIL-si-request"
    shipment = db_session.query(ShipmentRecord).one()
    assert shipment.shipment_key == "REF:SHP-021"
    assert len(_versions(db_session, shipment.id)) == 1
    overview = shipment_overview.build_overview(db_session, shipment)
    # Still incomplete: the SI is here, the BL is not.
    assert overview["status"] == "NEEDS_ATTENTION"
    assert overview["missing_documents"] == ["BL"]


def test_a_late_arriving_older_email_does_not_replace_the_current_version(
        monkeypatch, db_session):
    messages, blobs = three_si_messages()
    stub_gmail(monkeypatch, messages, blobs, ["si-v3", "si-v2", "si-v1"])
    sync.poll_and_process(db_session)

    shipment = db_session.query(ShipmentRecord).one()
    before = [v for v in _versions(db_session, shipment.id) if v.is_latest][0]

    older_id = "si-v0"
    stub_gmail(monkeypatch,
               {older_id: gmail_message(
                   older_id, "Shipping Instruction (earlier draft) - SHP-001",
                   "SHP-001_SI_v0.txt", "Thu, 17 Sep 2026 09:00:00 +0800")},
               {f"att-{older_id}": si_text("v1").replace(
                   "38,900 KG", "35,000 KG").encode()},
               [older_id])
    sync.poll_and_process(db_session)

    versions = _versions(db_session, shipment.id)
    assert len(versions) == 4
    assert versions[0].extracted_fields["gross_weight_kg"] == 35000.0
    assert versions[0].filename.endswith("SHP-001_SI_v0.txt")
    after = [v for v in versions if v.is_latest][0]
    assert after.id == before.id, "an older document must not become current"
    assert after.extracted_fields["gross_weight_kg"] == 41500.0


def test_missing_bl_keeps_the_shipment_incomplete(monkeypatch, db_session):
    """SI present, BL never arrives."""
    si_id = "only-si"
    stub_gmail(
        monkeypatch,
        {si_id: gmail_message(si_id, "Shipping Instruction - SHP-002 - please verify",
                              "SHP-002_SI.txt", "Sat, 19 Sep 2026 10:00:00 +0800")},
        {f"att-{si_id}": si_text("v1", ref="SHP-002").encode()},
        [si_id])
    sync.poll_and_process(db_session)

    shipment = db_session.query(ShipmentRecord).one()
    assert shipment.shipment_key == "REF:SHP-002"
    overview = shipment_overview.build_overview(db_session, shipment)
    assert overview["status"] == "NEEDS_ATTENTION"
    assert overview["status"] != "VERIFIED"
    assert overview["complete"] is False
    assert overview["missing_documents"] == ["BL"]
    assert any("Bill of Lading" in r for r in overview["reasons"])
    assert "REQUEST_BL" in overview["actions"]
    # No fake BL document may be created to make the shipment look complete.
    assert overview["documents"]["BL"]["present"] is False
    assert overview["documents"]["BL"]["version_count"] == 0
    assert _versions(db_session, shipment.id, "BL") == []


def test_get_endpoints_do_not_mutate_state(monkeypatch, db_session):
    """Reads must not process, create or renumber anything."""
    from fastapi.testclient import TestClient

    from app.main import app

    messages, blobs = three_si_messages()
    stub_gmail(monkeypatch, messages, blobs, ["si-v3", "si-v2", "si-v1"])
    sync.poll_and_process(db_session)

    # An email that has never been processed, to catch write-on-read.
    db_session.add(EmailRecord(email_id="UNPROCESSED-1", sender="a@b.com",
                               subject="Weekly berthing report",
                               body="Nothing to do here.", attachments=[]))
    db_session.commit()

    def snapshot():
        return (
            db_session.query(ReportRecord).count(),
            db_session.query(DocumentVersionRecord).count(),
            db_session.query(ShipmentRecord).count(),
            tuple(sorted((r.email_id, r.attempts, r.processing_ms)
                         for r in db_session.query(ReportRecord).all())),
            tuple(sorted((v.id, v.version_number, v.is_latest)
                         for v in db_session.query(DocumentVersionRecord).all())),
        )

    before = snapshot()
    client = TestClient(app)
    for _ in range(3):
        for url in ("/shipments", "/shipments/overview", "/emails?limit=50",
                    "/api/emails", "/api/summary", "/api/results?limit=8",
                    "/api/emails/UNPROCESSED-1"):
            response = client.get(url)
            assert response.status_code == 200, (url, response.status_code)

    db_session.expire_all()
    assert snapshot() == before, "a GET changed stored state"

    # The unprocessed email stayed unprocessed, and the API said so.
    payload = client.get("/api/emails/UNPROCESSED-1").json()
    assert payload["processed"] is False
    assert payload["result"] is None
    assert db_session.query(ReportRecord).filter_by(
        email_id="UNPROCESSED-1").first() is None


def test_gmail_date_header_is_parsed_into_received_at(monkeypatch, db_session):
    """The Date header drives chronology, so it must survive into the DB."""
    messages, blobs = three_si_messages()
    stub_gmail(monkeypatch, messages, blobs, ["si-v3", "si-v2", "si-v1"])
    sync.poll_and_process(db_session)

    # "Fri, 18 Sep 2026 09:00:00 +0800" is 2026-09-18T01:00:00Z.
    email = db_session.query(EmailRecord).filter_by(
        email_id="GMAIL-si-v1").one()
    assert email.received_at is not None
    assert email.received_at.isoformat().startswith("2026-09-18T01:00:00")

    shipment = db_session.query(ShipmentRecord).one()
    stamps = [v.received_at for v in _versions(db_session, shipment.id)]
    assert all(s is not None and s.tzinfo is None for s in stamps)
    assert stamps == sorted(stamps), "version timestamps must be chronological"


def test_gmail_internal_date_is_the_fallback_and_bad_dates_are_survivable(
        monkeypatch, db_session):
    from app.integrations.gmail.parser import _received_iso

    # internalDate (epoch ms) is used when there is no Date header.
    assert (_received_iso({"internalDate": 1758234000000}, {}) or "").startswith(
        "2025-09-18")
    # Malformed / missing headers must not raise.
    assert _received_iso({}, {"date": "not a date"}) is None
    assert _received_iso({}, {}) is None
    # A zoneless header is read as UTC, not as local time.
    assert _received_iso({}, {"date": "Wed, 16 Sep 2026 10:00:00"}) == \
        "2026-09-16T10:00:00+00:00"
    # Offsets are applied.
    assert _received_iso({}, {"date": "Fri, 19 Sep 2026 23:30:00 -0700"}) == \
        "2026-09-20T06:30:00+00:00"

    # A message with no usable timestamp is still ingested.
    msg_id = "no-date"
    stub_gmail(monkeypatch,
               {msg_id: gmail_message(msg_id, "Shipping Instruction - SHP-003",
                                      "SHP-003_SI.txt")},
               {f"att-{msg_id}": si_text("v1", ref="SHP-003").encode()},
               [msg_id])
    result = sync.poll_and_process(db_session)
    assert result["failed"] == 0
    assert db_session.query(ReportRecord).filter_by(
        email_id="GMAIL-no-date").first() is not None


def test_a_real_pdf_attachment_wins_over_the_email_body(monkeypatch, db_session):
    """B2: the attached document must be read, never substituted by the body."""
    fitz = pytest.importorskip("fitz", reason="PDF fixture needs PyMuPDF")

    def render(text: str) -> bytes:
        doc = fitz.open()
        page = doc.new_page()
        y = 60
        for line in text.splitlines():
            page.insert_text((60, y), line, fontsize=9.5, fontname="cour")
            y += 15
        data = doc.tobytes(deflate=True)
        doc.close()
        return data

    msg_id = "pdf-si"
    # The body quotes the full SI text, so a body-derived document is possible.
    messages = {msg_id: gmail_message(
        msg_id, "Shipping Instruction - SHP-004 - please check",
        "SI_v1.pdf", "Sat, 19 Sep 2026 09:00:00 +0800",
        body="Please check the attached instruction.\n\n"
             + si_text("v1", ref="SHP-004"))}
    stub_gmail(monkeypatch, messages,
               {f"att-{msg_id}": render(si_text("v1", ref="SHP-004"))}, [msg_id])
    sync.poll_and_process(db_session)

    shipment = db_session.query(ShipmentRecord).one()
    versions = _versions(db_session, shipment.id)
    assert len(versions) == 1
    version = versions[0]
    assert version.filename.endswith("SI_v1.pdf"), version.filename
    assert "frombody" not in version.filename
    assert version.extracted_fields["gross_weight_kg"] == 38900.0
    assert version.extracted_fields["shipper"] == "ABC LOGISTICS SDN BHD"
