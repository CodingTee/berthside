"""Tests for atomic batch-resolve and draft generation endpoint."""
from __future__ import annotations

import base64
from fastapi.testclient import TestClient

from app.main import app
from app.models import IssueRecord, ResolutionRecord, ShipmentRecord

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


def _bl_text(containers: str = "4 x 40'HC") -> str:
    return SI_TEXT.replace("SHIPPING INSTRUCTION", "BILL OF LADING").replace(
        "Shipper/Exporter:", "SHIPPER:").replace(
        "No. of Containers or Packages: 2 x 40'HC",
        f"Container Count: {containers}")


def test_batch_resolve_and_draft_generates_draft_atomically(db_session):
    client = TestClient(app)

    # 1. Ingest via /api/process to create the shipment with a mismatch
    payload = {
        "email_id": "shipmail-batch-1",
        "message_id": "shipmail-batch-1",
        "thread_id": "thread-batch-1",
        "from": "amelia.tan@abclogistics.com",
        "subject": "SHP-900 - please check SI and draft BL",
        "body": "Attached are the SI and draft BL. Please check and confirm.",
        "attachments": [
            {
                "filename": "SHP-900_SI.txt",
                "content_base64": base64.b64encode(SI_TEXT.encode()).decode(),
            },
            {
                "filename": "SHP-900_BL.txt",
                "content_base64": base64.b64encode(_bl_text("4 x 40'HC").encode()).decode(),
            },
        ],
        "source": "shipmail",
    }
    r = client.post("/api/process", json=payload)
    assert r.status_code == 200

    shipment = db_session.query(ShipmentRecord).first()
    assert shipment is not None

    # Verify an issue was opened for container_count mismatch
    issues = db_session.query(IssueRecord).filter_by(shipment_id=shipment.id).all()
    assert len(issues) >= 1

    # 2. Call batch-resolve-draft
    res = client.post(
        f"/api/shipments/{shipment.id}/batch-resolve-draft",
        json={
            "reviewed_by": "lead_operator",
            "review_comment": "Approved SI container count 2 over BL 4",
            "email_id": "shipmail-batch-1",
        },
    )
    assert res.status_code == 200
    data = res.json()
    assert data["shipment_id"] == shipment.id
    assert data["approved_count"] >= 1
    assert data["draft"] is not None
    assert "corrected_draft_path" in data["draft"]
    assert data["resolution_status"]["status"] in ("RESOLVED", "VERIFIED")

    # 3. Check DB records
    db_session.expire_all()
    for iss in db_session.query(IssueRecord).filter_by(shipment_id=shipment.id).all():
        assert iss.status == "APPROVED"
        resolution = db_session.query(ResolutionRecord).filter_by(issue_id=iss.id).first()
        assert resolution is not None
        assert resolution.status == "APPROVED"
        assert resolution.reviewed_by == "lead_operator"
