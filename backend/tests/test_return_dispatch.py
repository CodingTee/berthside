"""Tests for the Enterprise Hub return-to-sender dispatch.

Returning a processed email to its origin mailbox must:
* render a draft without side effects on dry_run,
* persist a DispatchRecord and flip the row to RETURNED on dispatch,
* generate a verification receipt for approved docs and a rejection notice
  for rejected docs,
* expose prior returns through the dispatch-history endpoint.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.database import get_db
from app.main import app
from app.models import DispatchRecord, StagedEmailRecord


@pytest.fixture
def client():
    return TestClient(app)


def _seed(db, stage_id="STG-TEST1", status="APPROVED", security="CLEAN"):
    rec = StagedEmailRecord(
        stage_id=stage_id,
        source_mailbox="sdoc-hackathon-bundle@averis.com",
        sender="shipper@fastocean.com",
        recipient="sdoc-hackathon-bundle@averis.com",
        subject="Booking BKG-8812 - Draft B/L",
        body="Please verify the attached draft B/L.",
        attachments=["Draft_BL.pdf"],
        security_status=security,
        security_details=[],
        category="BL_COMPARISON",
        confidence=1.0,
        ai_reason="Benchmark gold batch ingestion verified",
        status=status,
        ai_engine="cloud-cascade (gemini-2.5-flash)",
    )
    db.add(rec)
    db.commit()
    return rec


def test_dry_run_renders_draft_without_persisting(client, db_session):
    _seed(db_session, status="APPROVED")
    res = client.post(
        "/api/v1/gateway/emails/STG-TEST1/return",
        json={"dry_run": True},
    )
    assert res.status_code == 200, res.text
    d = res.json()
    assert d["dry_run"] is True
    assert d["reply_via"] == "sdoc-hackathon-bundle@averis.com"
    assert d["reply_to"] == "shipper@fastocean.com"
    assert d["decision"] == "VERIFIED"
    assert "Verification Complete" in d["subject"]
    # Nothing persisted on dry run.
    assert db_session.query(DispatchRecord).count() == 0


def test_dispatch_persists_and_marks_returned(client, db_session):
    _seed(db_session, status="APPROVED")
    res = client.post(
        "/api/v1/gateway/emails/STG-TEST1/return",
        json={"subject": "Re: custom", "body": "custom body"},
    )
    assert res.status_code == 200, res.text
    d = res.json()
    assert d["delivery"] == "SIMULATED"
    assert d["dispatch_id"]

    # A DispatchRecord was written and the staged row flipped to RETURNED.
    assert db_session.query(DispatchRecord).count() == 1
    rec = db_session.query(DispatchRecord).first()
    assert rec.stage_id == "STG-TEST1"
    assert rec.recipient == "shipper@fastocean.com"
    assert rec.decision == "VERIFIED"
    # The operator's edited text is what leaves, not the generated default.
    assert rec.subject == "Re: custom"
    assert rec.body == "custom body"
    staged = db_session.query(StagedEmailRecord).filter_by(stage_id="STG-TEST1").first()
    assert staged.status == "RETURNED"


def test_rejected_record_gets_rejection_receipt(client, db_session):
    _seed(db_session, status="REJECTED")
    res = client.post(
        "/api/v1/gateway/emails/STG-TEST1/return",
        json={"dry_run": True},
    )
    assert res.status_code == 200, res.text
    d = res.json()
    assert d["decision"] == "REJECTED"
    assert "Rejected" in d["subject"]


def test_dispatch_history_lists_returns(client, db_session):
    _seed(db_session, status="APPROVED")
    client.post("/api/v1/gateway/emails/STG-TEST1/return", json={"dry_run": False})
    client.post(
        "/api/v1/gateway/emails/STG-TEST1/return",
        json={"dry_run": False, "body": "second return"},
    )
    res = client.get("/api/v1/gateway/emails/STG-TEST1/dispatch-history")
    assert res.status_code == 200, res.text
    rows = res.json()
    assert len(rows) == 2
    assert all(r["channel"] == "sdoc-hackathon-bundle@averis.com" for r in rows)


def test_return_unknown_stage_404(client, db_session):
    res = client.post("/api/v1/gateway/emails/NOPE/return", json={"dry_run": True})
    assert res.status_code == 404


def test_mailto_url_generation(client, db_session):
    _seed(db_session, status="REJECTED")
    res = client.post(
        "/api/v1/gateway/emails/STG-TEST1/return",
        json={"dry_run": True},
    )
    assert res.status_code == 200
    d = res.json()
    assert "mailto_url" in d
    assert d["mailto_url"].startswith("mailto:shipper@fastocean.com?")
    assert "subject=" in d["mailto_url"]
    assert "body=" in d["mailto_url"]

