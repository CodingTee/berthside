"""Review-and-reply approval guarantees; SMTP is always mocked."""
from datetime import datetime, timedelta
from unittest.mock import patch
import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from app.database import Base
from app.models import EmailRecord, ReportRecord, GatewayPolicyRecord, OutboundApprovalRecord
from app.routers import frontend_compat as api

@pytest.fixture
def db():
    engine=create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(GatewayPolicyRecord(engine="rule",ingest_mode="manual",disposition_mode="manual"))
        session.add(EmailRecord(email_id="APPROVAL-TEST",sender="sender@example.test",source_mailbox="ops@example.test",subject="Test",body="Source",attachments=[]))
        session.add(ReportRecord(email_id="APPROVAL-TEST",category="BL_COMPARISON",status="MISMATCH"))
        session.commit()
        yield session
    engine.dispose()

def payload(**kw):
    return api.OutstreamReturnIn(subject="Reviewed subject",body="Reviewed content",**kw)

def approved(db):
    return api.approve_reply("APPROVAL-TEST",payload(),db)["approval_id"]

def test_manual_blocks_unapproved_and_preview_does_not_send(db):
    with patch("app.services.smtp_dispatcher.dispatch_smtp_email") as smtp:
        assert api.outstream_return("APPROVAL-TEST",payload(dry_run=True),db)["approval_required"]
        with pytest.raises(HTTPException) as exc:
            api.outstream_return("APPROVAL-TEST",payload(),db)
        assert exc.value.status_code == 409
        smtp.assert_not_called()

def test_edited_body_and_changed_recipient_invalidate_approval(db):
    token=approved(db)
    with patch("app.services.smtp_dispatcher.dispatch_smtp_email") as smtp:
        with pytest.raises(HTTPException):
            api.outstream_return("APPROVAL-TEST",api.OutstreamReturnIn(subject="Reviewed subject",body="Changed",approval_id=token),db)
        row=db.query(EmailRecord).first();row.sender="other@example.test";db.commit()
        with pytest.raises(HTTPException):api.outstream_return("APPROVAL-TEST",payload(approval_id=token),db)
        smtp.assert_not_called()

def test_approved_send_is_single_use_and_records_history(db):
    token=approved(db)
    with patch("app.services.smtp_dispatcher.dispatch_smtp_email",return_value={"status":"OK","delivery":"SENT_SMTP"}) as smtp:
        result=api.outstream_return("APPROVAL-TEST",payload(approval_id=token),db)
        with pytest.raises(HTTPException):api.outstream_return("APPROVAL-TEST",payload(approval_id=token),db)
        smtp.assert_called_once()
        audit=api.reply_approvals("APPROVAL-TEST",db)[0]
        assert audit["dispatch_id"]==result["dispatch_id"] and audit["status"]=="SENT_SMTP"

def test_failure_requires_new_approval(db):
    token=approved(db)
    with patch("app.services.smtp_dispatcher.dispatch_smtp_email",return_value={"status":"ERROR","error":"Test failure"}):
        assert api.outstream_return("APPROVAL-TEST",payload(approval_id=token),db)["delivery"]=="FAILED"
        with pytest.raises(HTTPException):api.outstream_return("APPROVAL-TEST",payload(approval_id=token),db)
    assert approved(db)!=token

def test_expired_and_superseded_approvals_blocked(db):
    old=approved(db);new=approved(db)
    with patch("app.services.smtp_dispatcher.dispatch_smtp_email") as smtp:
        with pytest.raises(HTTPException):api.outstream_return("APPROVAL-TEST",payload(approval_id=old),db)
        row=db.get(OutboundApprovalRecord,new);row.created_at=datetime.utcnow()-timedelta(days=2);db.commit()
        with pytest.raises(HTTPException):api.outstream_return("APPROVAL-TEST",payload(approval_id=new),db)
        smtp.assert_not_called()

def test_effective_auto_policy_and_manual_override(db):
    pol=db.query(GatewayPolicyRecord).first();pol.disposition_mode="auto";db.commit()
    with patch("app.services.smtp_dispatcher.dispatch_smtp_email",return_value={"status":"OK","delivery":"SIMULATED"}) as smtp:
        api.outstream_return("APPROVAL-TEST",payload(),db)
        row=db.query(EmailRecord).first();row.disposition_override="MANUAL";db.commit()
        with pytest.raises(HTTPException):api.outstream_return("APPROVAL-TEST",payload(),db)
        smtp.assert_called_once()

def test_uncertain_transport_cannot_reuse_approval(db):
    token=approved(db)
    with patch("app.services.smtp_dispatcher.dispatch_smtp_email",side_effect=RuntimeError("transport")) as smtp:
        with pytest.raises(HTTPException):api.outstream_return("APPROVAL-TEST",payload(approval_id=token),db)
        assert db.get(OutboundApprovalRecord,token).status=="UNCONFIRMED"
        with pytest.raises(HTTPException):api.outstream_return("APPROVAL-TEST",payload(approval_id=token),db)
        smtp.assert_called_once()


def test_legacy_gateway_cannot_bypass_manual_policy(db):
    from types import SimpleNamespace
    from app.routers.gateway import _require_auto_outbound
    staged=SimpleNamespace(stage_id="TEST",source_mailbox="ops@example.test")
    with pytest.raises(HTTPException) as exc:_require_auto_outbound(staged,db)
    assert exc.value.status_code==409
    pol=db.query(GatewayPolicyRecord).first();pol.disposition_policies={"ops@example.test":"auto"};db.commit()
    _require_auto_outbound(staged,db)
    db.add(EmailRecord(email_id="INGEST-TEST",sender="sender@example.test",disposition_override="MANUAL"));db.commit()
    with pytest.raises(HTTPException):_require_auto_outbound(staged,db)
