"""The ShipMail "Run ShipSync" flow must persist shipment state (J6).

The button posts to `POST /api/process`. That endpoint used to stop at the
verdict, so the shipment it had just verified was invisible to `/shipments`.
This drives the real request through the app and asserts the shipment exists
afterwards — and that pressing the button again does not duplicate anything.
"""
from __future__ import annotations

import base64

from fastapi.testclient import TestClient

from app.main import app
from app.models import (DocumentVersionRecord, EmailRecord, ReportRecord,
                        ShipmentRecord)
from app.services import shipment_overview

SI_TEXT = """SHIPPING INSTRUCTION
Shipper/Exporter: ABC LOGISTICS SDN BHD
CONSIGNEE: ROTTERDAM TRADING BV
NOTIFY PARTY: ROTTERDAM TRADING BV
Port of Loading: PORT KLANG, MALAYSIA (MYPKG)
Discharge Port: ROTTERDAM, NETHERLANDS (NLRTM)
No. of Containers or Packages: 2 x 40'HC
Gross Weight (KG): 41,500 KG
Vessel Name: EVER GLORY
Voy. No: 026E
Container No.: ABCU1234567
Shipment ID: SHP-900
Freight: PREPAID
"""


def _bl_text(containers: str = "2 x 40'HC") -> str:
    return SI_TEXT.replace("SHIPPING INSTRUCTION", "BILL OF LADING").replace(
        "Shipper/Exporter:", "SHIPPER:").replace(
        "No. of Containers or Packages: 2 x 40'HC",
        f"Container Count: {containers}")


def _payload(email_id: str = "shipmail-btn-1") -> dict:
    return {
        "email_id": email_id,
        "message_id": email_id,
        "thread_id": "thread-1",
        "from": "amelia.tan@abclogistics.com",
        "subject": "SHP-900 - please check SI and draft BL",
        "body": "Attached are the SI and draft BL. Please check and confirm.",
        "attachments": [
            {"filename": "SHP-900_SI.txt",
             "content_base64": base64.b64encode(SI_TEXT.encode()).decode()},
            {"filename": "SHP-900_BL.txt",
             "content_base64": base64.b64encode(_bl_text().encode()).decode()},
        ],
        "source": "shipmail",
    }


def test_run_shipsync_creates_the_shipment_that_shipments_endpoint_shows(db_session):
    client = TestClient(app)
    response = client.post("/api/process", json=_payload())
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["category"] == "BL_COMPARISON"
    assert result["status"] == "OK"

    # What the button click must leave behind...
    assert db_session.query(ShipmentRecord).count() == 1, \
        "POST /api/process did not create a shipment"
    assert db_session.query(EmailRecord).filter_by(
        email_id="shipmail-btn-1").one() is not None
    reports = db_session.query(ReportRecord).all()
    assert len(reports) == 1
    assert reports[0].status == "OK"

    # ...and what `/shipments` must therefore be able to show.
    listed = client.get("/shipments/by-key/SHP-900")
    assert listed.status_code == 200, listed.text
    overview = listed.json()
    assert overview["status"] == "VERIFIED"
    assert overview["documents"]["SI"]["present"] is True
    assert overview["documents"]["BL"]["present"] is True
    assert overview["documents"]["SI"]["version_count"] == 1
    assert overview["documents"]["BL"]["version_count"] == 1
    assert [e["email_id"] for e in overview["source_emails"]] == ["shipmail-btn-1"]


def test_pressing_run_shipsync_again_does_not_duplicate_anything(db_session):
    client = TestClient(app)
    client.post("/api/process", json=_payload())

    before = (db_session.query(ShipmentRecord).count(),
              db_session.query(DocumentVersionRecord).count(),
              db_session.query(ReportRecord).count())

    second = client.post("/api/process", json=_payload())
    assert second.status_code == 200
    assert second.json()["cached"] is True, "an unchanged email must be reused"

    db_session.expire_all()
    after = (db_session.query(ShipmentRecord).count(),
             db_session.query(DocumentVersionRecord).count(),
             db_session.query(ReportRecord).count())
    assert after == before, f"the second click changed stored state: {before} -> {after}"


def test_a_mismatch_is_persisted_and_visible_on_the_shipment(db_session):
    client = TestClient(app)
    payload = _payload("shipmail-btn-2")
    payload["attachments"][1] = {
        "filename": "SHP-900_BL.txt",
        "content_base64": base64.b64encode(
            _bl_text(containers="4 x 40'HC").encode()).decode(),
    }
    response = client.post("/api/process", json=payload)
    assert response.json()["status"] == "MISMATCH"

    shipment = db_session.query(ShipmentRecord).one()
    overview = shipment_overview.build_overview(db_session, shipment)
    assert overview["status"] == "NEEDS_ATTENTION"
    assert overview["mismatch_fields"] == ["container_count"]
    assert overview["issues"], "the mismatch must be stored as an issue row"


def test_a_stateless_analysis_still_persists_nothing(db_session):
    """`/api/v1/analyze` is the explicitly stateless sibling — keep it that way."""
    client = TestClient(app)
    response = client.post("/api/v1/analyze", json=_payload("analyze-only"))
    assert response.status_code == 200
    assert response.json()["status"] == "OK"
    assert db_session.query(ShipmentRecord).count() == 0
    assert db_session.query(ReportRecord).count() == 0
