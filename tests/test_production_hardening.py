"""Tests for the production hardening: safe dispatch, trace ids and DSN receipts."""
from __future__ import annotations

import logging
from email import message_from_string
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from app import tracing
from app.main import app
from app.models import DeliveryEventRecord, DispatchRecord, StagedEmailRecord


@pytest.fixture
def client():
    return TestClient(app)


def _seed_stage(db, stage_id):
    db.add(StagedEmailRecord(
        stage_id=stage_id,
        source_mailbox="sdoc-hackathon-bundle@averis.com",
        sender="operator-forwarder@test.com",
        recipient="sdoc-hackathon-bundle@averis.com",
        subject="Fwd: Draft BL BKG-4455 for Verification",
        body=(
            "---------- Forwarded message ---------\n"
            "From: Alice <shipper@exportcorp.com>\n"
            "Subject: Draft BL\n\nPlease find draft BL attached."
        ),
        attachments=["Draft_BL.pdf"],
        security_status="CLEAN",
        category="BL_COMPARISON",
        status="APPROVED",
    ))
    db.commit()


def _dsn(original_message_id, recipient, action="failed", status="5.1.1"):
    return f"""From: Mail Delivery Subsystem <MAILER-DAEMON@mx.example.com>
To: ops@berthside.demo
Subject: Delivery Status Notification (Failure)
Message-ID: <dsn-001@mx.example.com>
MIME-Version: 1.0
Content-Type: multipart/report; report-type=delivery-status; boundary="BOUND"

--BOUND
Content-Type: text/plain; charset=utf-8

The original message could not be delivered.

--BOUND
Content-Type: message/delivery-status

Original-Message-ID: {original_message_id}
Final-Recipient: rfc822; {recipient}
Action: {action}
Status: {status}
Diagnostic-Code: smtp; 550 5.1.1 user unknown

--BOUND
Content-Type: message/rfc822

From: ops@berthside.demo
To: {recipient}
Subject: Re: Draft BL - Document Verification Complete - Ref #STG-HARD-1
Message-ID: {original_message_id}

Original body.

--BOUND--
""".encode("utf-8")


def test_confirmed_post_dispatches_once(client, db_session):
    """A replayed confirmation must not send a second customer email."""
    _seed_stage(db_session, "STG-HARD-1")
    payload = {"stage_id": "STG-HARD-1", "recipient": "client@shipper.com"}

    with patch("app.services.smtp_dispatcher.dispatch_smtp_email") as smtp:
        smtp.return_value = {
            "delivery": "SENT_SMTP", "channel": "SMTP", "message_id": "<out-1@hub>",
        }
        first = client.post("/api/v1/gateway/customer-notice/send", data=payload)
        assert first.status_code == 200
        assert "Official Customer Notice Dispatched Successfully" in first.text
        smtp.assert_called_once()

        rec = db_session.query(DispatchRecord).filter_by(stage_id="STG-HARD-1").first()
        assert rec is not None
        assert rec.message_id == "<out-1@hub>"
        assert rec.delivery_state == "UNKNOWN"
        assert rec.trace_id

        replay = client.post("/api/v1/gateway/customer-notice/send", data=payload)
        assert replay.status_code == 200
        assert "no duplicate sent" in replay.text
        smtp.assert_called_once()
        assert db_session.query(DispatchRecord).filter_by(stage_id="STG-HARD-1").count() == 1


def test_different_recipient_is_not_a_duplicate(client, db_session):
    """Changing the recipient is a real new notice, not a replay."""
    _seed_stage(db_session, "STG-HARD-2")
    with patch("app.services.smtp_dispatcher.dispatch_smtp_email") as smtp:
        smtp.return_value = {"delivery": "SENT_SMTP", "channel": "SMTP"}
        one = client.post(
            "/api/v1/gateway/customer-notice/dispatch",
            json={"stage_id": "STG-HARD-2", "recipient": "a@shipper.com"},
        )
        two = client.post(
            "/api/v1/gateway/customer-notice/dispatch",
            json={"stage_id": "STG-HARD-2", "recipient": "b@shipper.com"},
        )
        assert one.json()["status"] == "SUCCESS"
        assert two.json()["status"] == "SUCCESS"
        assert smtp.call_count == 2


def test_request_id_is_honoured_and_generated(client):
    """The middleware exposes the caller's trace id or mints a short one."""
    given = client.get("/health", headers={"X-Request-ID": "trace-abc-123"})
    assert given.headers["X-Request-ID"] == "trace-abc-123"

    minted = client.get("/health")
    assert len(minted.headers["X-Request-ID"]) == 12

    unsafe = client.get("/health", headers={"X-Request-ID": "bad\nid"})
    assert unsafe.headers["X-Request-ID"] != "bad\nid"


def test_correlation_ids_attached_to_log_records(caplog):
    """Every LogRecord carries the active ids, whatever handler formats it."""
    trace_token = tracing.set_trace_id("trace-xyz")
    entity_token = tracing.set_entity_id("EXT-77")
    try:
        with caplog.at_level(logging.INFO):
            logging.getLogger("probe").info("hello")
        record = caplog.records[-1]
        assert record.trace_id == "trace-xyz"
        assert record.entity_id == "EXT-77"
    finally:
        tracing.reset_trace_id(trace_token)
        tracing.reset_entity_id(entity_token)


def test_clean_id_rejects_unsafe_values():
    assert tracing.clean_id("plain-id-42") == "plain-id-42"
    assert tracing.clean_id("bad\nvalue") == ""
    assert tracing.clean_id("") == ""
    assert tracing.clean_id("x" * 200) == ""


def test_dsn_ingest_updates_dispatch_state(client, db_session):
    """A bounce must write back to the dispatch that produced it."""
    _seed_stage(db_session, "STG-HARD-1")
    with patch("app.services.smtp_dispatcher.dispatch_smtp_email") as smtp:
        smtp.return_value = {
            "delivery": "SENT_SMTP", "channel": "SMTP", "message_id": "<out-1@hub>",
        }
        client.post(
            "/api/v1/gateway/customer-notice/send",
            data={"stage_id": "STG-HARD-1", "recipient": "client@shipper.com"},
        )

    raw = _dsn("<out-1@hub>", "client@shipper.com")
    res = client.post(
        "/api/v1/gateway/delivery-receipts/ingest",
        content=raw,
        headers={"Content-Type": "message/rfc822"},
    )
    assert res.status_code == 200, res.text
    data = res.json()
    assert data["status"] == "RECORDED"
    assert data["event_type"] == "BOUNCED"
    assert data["matched"] is True

    rec = db_session.query(DispatchRecord).filter_by(stage_id="STG-HARD-1").first()
    assert rec.delivery_state == "BOUNCED"
    assert rec.delivery_detail == "smtp; 550 5.1.1 user unknown"

    state = client.get("/api/v1/gateway/delivery-status?stage_id=STG-HARD-1")
    assert state.status_code == 200
    assert state.json()["dispatches"][0]["delivery_state"] == "BOUNCED"

    listing = client.get("/api/v1/gateway/delivery-receipts?stage_id=STG-HARD-1")
    assert listing.status_code == 200
    assert listing.json()["count"] == 1
    assert listing.json()["events"][0]["status_code"] == "5.1.1"

    again = client.post(
        "/api/v1/gateway/delivery-receipts/ingest",
        content=raw,
        headers={"Content-Type": "message/rfc822"},
    )
    assert again.json()["status"] == "DUPLICATE"
    assert db_session.query(DeliveryEventRecord).count() == 1


def test_dsn_ingest_matches_by_stage_reference(client, db_session):
    """Even without a Message-ID, the Ref # in the original subject links it."""
    _seed_stage(db_session, "STG-HARD-1")
    with patch("app.services.smtp_dispatcher.dispatch_smtp_email") as smtp:
        smtp.return_value = {"delivery": "SIMULATED", "channel": "SIMULATED"}
        client.post(
            "/api/v1/gateway/customer-notice/send",
            data={"stage_id": "STG-HARD-1", "recipient": "client@shipper.com"},
        )

    raw = _dsn("<unknown@elsewhere>", "unknown@example.com", action="delayed", status="4.2.1")
    res = client.post("/api/v1/gateway/delivery-receipts/ingest", content=raw)
    assert res.status_code == 200
    assert res.json()["event_type"] == "DELAYED"
    assert res.json()["matched"] is True


def test_non_dsn_is_rejected(client):
    plain = message_from_string(
        "From: shipper@exportcorp.com\nSubject: Draft BL for verification\n\nPlease review.\n"
    )
    res = client.post(
        "/api/v1/gateway/delivery-receipts/ingest", content=plain.as_bytes()
    )
    assert res.status_code == 400


def test_error_rows_past_attempt_cap_retire_to_dead_letter(db_session):
    """An ERROR report that has exhausted its retries is retired so it cannot
    spin forever, while a NEEDS_REVIEW row is left for a human and never
    retired to DEAD_LETTER."""
    from app.config import get_settings
    from app.models import ReportRecord
    from app.services import workflow

    cap = get_settings().retry_max_attempts
    burnt = ReportRecord(
        email_id="DL-BURNT", category="GENERAL", status="ERROR",
        attempts=cap + 2, error_message="permanent failure",
    )
    human = ReportRecord(
        email_id="DL-HUMAN", category="BL_COMPARISON", status="NEEDS_REVIEW",
        attempts=cap + 2, review_reason="missing_attachment",
    )
    db_session.add_all([burnt, human])
    db_session.commit()

    result = workflow.retry_failed(db_session, statuses=["ERROR", "NEEDS_REVIEW"])

    db_session.refresh(burnt)
    db_session.refresh(human)
    assert burnt.status == "DEAD_LETTER"
    assert result["dead_lettered"] == 1
    # A human-escalation status is never retired to DEAD_LETTER.
    assert human.status == "NEEDS_REVIEW"

    listed = workflow.dead_letters(db_session)
    assert any(r["email_id"] == "DL-BURNT" for r in listed)
    assert all(r["email_id"] != "DL-HUMAN" for r in listed)
