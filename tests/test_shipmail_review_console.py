"""Tests for the ShipMail review console (the OAuth workspace's own console).

The hub distributes mail per source mailbox; this workspace has a single source,
so distribution is per email. What must hold:

* the review payload counts and lists only messages stored in the OAuth
  database, and the counters stay whole while the table is filtered;
* an assignment is an upsert, and an email_id the workspace has never seen is
  refused instead of written;
* a bulk run honours the operator's distribution, so IGNORE rows are not
  processed behind their back;
* returning an outcome renders a draft without side effects, and the real call
  writes an auditable DispatchRecord with the text the operator approved.

Every request runs against an isolated in-memory OAuth database, so the
developer's sdoc_oauth.db is never written to.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base, get_oauth_db
from app.models import (
    DispatchRecord,
    EmailRecord,
    GmailMessageRecord,
    ReportRecord,
    ShipmailAssignmentRecord,
)


@pytest.fixture()
def oauth_session():
    """An isolated OAuth database, wired into the app for this test only."""
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autocommit=False, autoflush=False)

    from app.main import app as fastapi_app

    def _oauth_session():
        db = factory()
        try:
            yield db
        finally:
            db.close()

    fastapi_app.dependency_overrides[get_oauth_db] = _oauth_session
    try:
        yield factory
    finally:
        fastapi_app.dependency_overrides.pop(get_oauth_db, None)
        engine.dispose()


@pytest.fixture()
def client(oauth_session):
    from app.main import app as fastapi_app

    return TestClient(fastapi_app)


def _seed(factory, email_id, *, status="MISMATCH", category="BL_COMPARISON",
          defect_fields=("container_count",), missing=(), review_reason="missing_value",
          attachments=("data/gmail/x/SI.pdf",)):
    with factory() as db:
        db.add(EmailRecord(
            email_id=email_id,
            sender="ops@customer.example",
            subject=f"Shipment documents for {email_id}",
            body="Please find the shipping documents attached.",
            attachments=list(attachments),
            source_mailbox="operations@berthside.demo",
        ))
        db.add(GmailMessageRecord(
            gmail_message_id="gm-" + email_id,
            email_id=email_id,
            sender="ops@customer.example",
            subject=f"Shipment documents for {email_id}",
            processing_status="PROCESSED",
        ))
        db.add(ReportRecord(
            email_id=email_id,
            category=category,
            status=status,
            has_defect=1 if defect_fields else 0,
            defect_fields=list(defect_fields),
            review_reason=review_reason,
            extracted={"missing_documents": list(missing), "shipment_key": "REF:SHP-001"},
        ))
        db.commit()


def test_review_payload_counts_and_lists_workspace_mail(client, oauth_session):
    _seed(oauth_session, "GMAIL-1", status="MISMATCH")
    _seed(oauth_session, "GMAIL-2", status="OK", defect_fields=(), review_reason=None)

    res = client.get("/api/gmail/review")
    assert res.status_code == 200, res.text
    body = res.json()

    assert body["kpis"]["total"] == 2
    assert body["kpis"]["processed"] == 2
    assert body["kpis"]["mismatch"] == 1
    # Nothing has been distributed yet, so everything sits on the human queue.
    assert body["kpis"]["awaiting"] == 2
    assert body["total"] == 2

    row = next(r for r in body["items"] if r["email_id"] == "GMAIL-1")
    assert row["category"] == "BL_COMPARISON"
    assert row["status"] == "MISMATCH"
    assert row["assignment"] == "HOLD"
    assert row["defect_fields"] == ["container_count"]
    assert row["shipment"] == "SHP-001"
    assert row["n_attachments"] == 1
    assert row["attachments"] == ["SI.pdf"]
    assert row["dispatched"] is False


def test_review_filter_narrows_rows_but_not_counters(client, oauth_session):
    _seed(oauth_session, "GMAIL-1", status="MISMATCH")
    _seed(oauth_session, "GMAIL-2", status="OK", defect_fields=(), category="GENERAL")

    client.post("/api/gmail/assign",
                json={"email_ids": ["GMAIL-2"], "assignment": "IGNORE"})

    res = client.get("/api/gmail/review", params={"assignment": "IGNORE"})
    body = res.json()
    assert body["total"] == 1
    assert [r["email_id"] for r in body["items"]] == ["GMAIL-2"]
    # Counters describe the whole workspace, not the filtered slice.
    assert body["kpis"]["total"] == 2
    assert body["counts"]["assignment"]["IGNORE"] == 1
    assert body["counts"]["assignment"]["HOLD"] == 1

    by_category = client.get("/api/gmail/review", params={"category": "GENERAL"}).json()
    assert [r["email_id"] for r in by_category["items"]] == ["GMAIL-2"]

    by_status = client.get("/api/gmail/review", params={"status": "MISMATCH"}).json()
    assert [r["email_id"] for r in by_status["items"]] == ["GMAIL-1"]

    by_query = client.get("/api/gmail/review", params={"q": "GMAIL-1"}).json()
    assert [r["email_id"] for r in by_query["items"]] == ["GMAIL-1"]


def test_assign_upserts_and_refuses_mail_the_workspace_never_saw(client, oauth_session):
    _seed(oauth_session, "GMAIL-1")

    first = client.post("/api/gmail/assign",
                        json={"email_ids": ["GMAIL-1"], "assignment": "AUTO"})
    assert first.status_code == 200, first.text
    assert first.json()["updated"] == 1

    second = client.post("/api/gmail/assign",
                         json={"email_ids": ["GMAIL-1", "EMAIL-hub-1"], "assignment": "IGNORE",
                               "note": "not actionable"})
    assert second.status_code == 200, second.text
    assert second.json()["updated"] == 1
    assert second.json()["rejected"] == ["EMAIL-hub-1"]

    with oauth_session() as db:
        rows = db.query(ShipmailAssignmentRecord).all()
        assert len(rows) == 1, "assigning twice must update, not duplicate"
        assert rows[0].assignment == "IGNORE"
        assert rows[0].note == "not actionable"


def test_assign_rejects_an_unknown_value(client, oauth_session):
    _seed(oauth_session, "GMAIL-1")
    res = client.post("/api/gmail/assign",
                      json={"email_ids": ["GMAIL-1"], "assignment": "MAYBE"})
    assert res.status_code == 400


def test_bulk_run_skips_ignored_mail(client, oauth_session):
    _seed(oauth_session, "GMAIL-1")
    _seed(oauth_session, "GMAIL-2")
    client.post("/api/gmail/assign",
                json={"email_ids": ["GMAIL-2"], "assignment": "IGNORE"})

    res = client.post("/api/gmail/bulk-run",
                      json={"email_ids": ["GMAIL-1", "GMAIL-2", "GMAIL-404"]})
    assert res.status_code == 200, res.text
    body = res.json()

    reasons = {s["email_id"]: s["reason"] for s in body["skipped"]}
    assert reasons["GMAIL-2"] == "assigned IGNORE"
    assert reasons["GMAIL-404"] == "not in this workspace"
    assert body["processed"] == 1
    assert body["results"][0]["email_id"] == "GMAIL-1"

    # Asking for them explicitly is the only way they run.
    forced = client.post("/api/gmail/bulk-run",
                         json={"email_ids": ["GMAIL-2"], "include_ignored": True})
    assert forced.json()["processed"] == 1


def test_return_dry_run_renders_but_persists_nothing(client, oauth_session):
    _seed(oauth_session, "GMAIL-1", status="OK", defect_fields=(), review_reason=None)

    res = client.post("/api/gmail/return", params={"email_id": "GMAIL-1"},
                      json={"dry_run": True})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["dry_run"] is True
    assert body["decision"] == "VERIFIED"
    assert body["reply_to"] == "ops@customer.example"
    assert body["subject"] and body["body"]

    with oauth_session() as db:
        assert db.query(DispatchRecord).count() == 0


def test_return_receipt_follows_the_outcome(client, oauth_session):
    _seed(oauth_session, "GMAIL-OK", status="OK", defect_fields=(), review_reason=None)
    _seed(oauth_session, "GMAIL-DIFF", status="MISMATCH")
    _seed(oauth_session, "GMAIL-MISS", status="SKIPPED", category="SI_REQUEST",
          defect_fields=(), review_reason="missing_attachment", missing=("BL",))

    ok = client.post("/api/gmail/return", params={"email_id": "GMAIL-OK"},
                     json={"dry_run": True}).json()
    assert ok["decision"] == "VERIFIED"
    assert "Verification complete" in ok["subject"]

    diff = client.post("/api/gmail/return", params={"email_id": "GMAIL-DIFF"},
                       json={"dry_run": True}).json()
    assert diff["decision"] == "REJECTED"
    assert "container_count" in diff["body"]

    miss = client.post("/api/gmail/return", params={"email_id": "GMAIL-MISS"},
                       json={"dry_run": True}).json()
    assert miss["decision"] == "REJECTED"
    assert miss["missing_documents"] == ["BL"]
    assert "BL" in miss["subject"]


def test_return_dispatches_and_lands_in_the_audit_trail(client, oauth_session):
    _seed(oauth_session, "GMAIL-1")

    res = client.post("/api/gmail/return", params={"email_id": "GMAIL-1"},
                      json={"subject": "Re: edited", "body": "operator wording"})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["delivery"] == "SIMULATED"
    assert body["dispatch_id"]

    with oauth_session() as db:
        rec = db.query(DispatchRecord).one()
        assert rec.stage_id == "GMAIL-1"
        assert rec.recipient == "ops@customer.example"
        assert rec.channel == "SIMULATED"
        assert rec.gmail_message_id == "gm-GMAIL-1"
        # The operator's wording is what leaves.
        assert rec.subject == "Re: edited"
        assert rec.body == "operator wording"

    history = client.get("/api/gmail/dispatches").json()
    assert history["total"] == 1
    assert history["items"][0]["email_id"] == "GMAIL-1"

    # The row now reports that an outcome went back to the sender.
    row = next(r for r in client.get("/api/gmail/review").json()["items"]
               if r["email_id"] == "GMAIL-1")
    assert row["dispatched"] is True


def test_return_unknown_email_is_404(client, oauth_session):
    res = client.post("/api/gmail/return", params={"email_id": "GMAIL-NOPE"},
                      json={"dry_run": True})
    assert res.status_code == 404


def test_console_writes_never_reach_the_hub_database(client, oauth_session, db_session):
    _seed(oauth_session, "GMAIL-1")
    client.post("/api/gmail/assign", json={"email_ids": ["GMAIL-1"], "assignment": "AUTO"})
    client.post("/api/gmail/return", params={"email_id": "GMAIL-1"}, json={})

    # db_session is the isolated *hub* database from conftest.
    assert db_session.query(ShipmailAssignmentRecord).count() == 0
    assert db_session.query(EmailRecord).filter_by(email_id="GMAIL-1").first() is None
    assert db_session.query(DispatchRecord).count() == 0

    # And the hub console cannot see operator mail.
    hub = client.get("/api/v1/gateway/emails?limit=1000").json()
    assert all("GMAIL-1" != row.get("email_id") for row in hub)
