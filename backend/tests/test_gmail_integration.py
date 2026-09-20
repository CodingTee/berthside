"""Tests for real-Gmail integration plumbing without calling Google APIs."""
from __future__ import annotations

import base64

from fastapi.testclient import TestClient

from app.integrations.gmail import sync
from app.main import app
from app.models import GmailMessageRecord, ReportRecord


SI_TEXT = """SHIPPING INSTRUCTION
Shipper: EAST BRIGHT ENTERPRISE CO LTD
Consignee: OCEANIC LOGISTICS GMBH
Notify Party: PACIFIC FREIGHT SERVICES
Port of Loading: NANTONG, CHINA (CNNTG)
Port of Discharge: ROTTERDAM, NETHERLANDS (NLRTM)
Total Containers: 3 x 40'HC
Gross Weight: 22,000 KG
"""

BL_TEXT = """BILL OF LADING (DRAFT)
Shipper: EAST BRIGHT ENTERPRISE CO LTD
Consignee: OCEANIC LOGISTICS GMBH
Notify Party: PACIFIC FREIGHT SERVICES
Port of Loading: NANTONG, CHINA (CNNTG)
Port of Discharge: ROTTERDAM, NETHERLANDS (NLRTM)
Total Containers: 4 x 40'HC
Gross Weight: 22,000 KG
"""


def _gmail_message():
    return {
        "id": "msg-001",
        "threadId": "thread-001",
        "historyId": "42",
        "payload": {
            "headers": [
                {"name": "From", "value": "customer@example.com"},
                {"name": "Subject", "value": "SHP-001 SI and BL for checking"},
            ],
            "parts": [
                {
                    "mimeType": "text/plain",
                    "body": {
                        "data": base64.urlsafe_b64encode(
                            b"Please compare the SI and BL."
                        ).decode("ascii")
                    },
                },
                {
                    "filename": "Shipping_Instruction_SHP-001.txt",
                    "body": {"attachmentId": "att-si"},
                },
                {
                    "filename": "Draft_BL_SHP-001.txt",
                    "body": {"attachmentId": "att-bl"},
                },
            ],
        },
    }


def test_gmail_status_endpoint_reports_safe_configuration():
    client = TestClient(app)
    res = client.get("/api/gmail/status")
    assert res.status_code == 200, res.text
    data = res.json()
    assert data["account"] == "averis.demo@gmail.com"
    assert "token_configured" in data


def test_gmail_sync_processes_unseen_message(monkeypatch, db_session):
    from app.integrations.gmail import client as gmail_client

    monkeypatch.setattr(gmail_client, "list_message_ids", lambda max_results=None, query=None: ["msg-001"])
    monkeypatch.setattr(gmail_client, "get_message", lambda message_id: _gmail_message())
    monkeypatch.setattr(
        gmail_client,
        "get_attachment",
        lambda message_id, attachment_id: SI_TEXT.encode() if attachment_id == "att-si" else BL_TEXT.encode(),
    )

    result = sync.poll_and_process(db_session, limit=10)

    assert result["processed"] == 1
    report = db_session.query(ReportRecord).filter_by(email_id="GMAIL-msg-001").first()
    assert report is not None
    assert report.status == "MISMATCH"
    assert "container_count" in report.defect_fields

    record = db_session.query(GmailMessageRecord).filter_by(gmail_message_id="msg-001").first()
    assert record is not None
    assert record.thread_id == "thread-001"
    assert record.processing_status == "PROCESSED"

    second = sync.poll_and_process(db_session, limit=10)
    assert second["processed"] == 0
    assert second["skipped"] == 1


def test_gmail_credentials_rejects_invalid_payload():
    client = TestClient(app)
    res = client.post("/api/gmail/credentials", json={"invalid": "payload"})
    assert res.status_code == 400
    assert "Invalid Google OAuth client JSON format" in res.json()["detail"]


def test_gmail_credentials_accepts_valid_web_payload(tmp_path, monkeypatch):
    from app.config import get_settings

    dummy_path = tmp_path / "google_oauth_client.json"
    settings = get_settings()
    monkeypatch.setattr(settings, "gmail_credentials_file", str(dummy_path))

    client = TestClient(app)
    valid_payload = {
        "web": {
            "client_id": "test-client-id.apps.googleusercontent.com",
            "project_id": "test-project",
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "client_secret": "test-secret",
            "redirect_uris": ["http://127.0.0.1:8000/api/gmail/oauth-callback"],
        }
    }
    res = client.post("/api/gmail/credentials", json=valid_payload)
    assert res.status_code == 200
    assert res.json()["status"] == "configured"
    assert dummy_path.is_file()

