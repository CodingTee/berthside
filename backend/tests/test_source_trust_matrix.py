"""Unit tests for Source Trust Matrix filtering."""
import pytest
from app.models import EmailRecord, GatewayPolicyRecord, StagedEmailRecord
from app.services.imap_poller import _stage_and_verify_single_email
from app.services.trust_matrix import is_source_trusted, get_all_trusted_sources


def test_enterprise_intake_source_is_trusted_by_default(db_session):
    """Ensure averis.demo@gmail.com is in the matrix and passes the trust gate."""
    trusted, reason = is_source_trusted("averis.demo@gmail.com", db=db_session)
    assert trusted is True
    assert "verified in Source Trust Matrix" in reason


def test_personal_gmail_senders_are_not_trusted_by_default(db_session):
    """Personal Gmail mailboxes ship unregistered; only matrix entries pass."""
    trusted, reason = is_source_trusted("someone.private@gmail.com", db=db_session)
    assert trusted is False
    assert "not registered in Source Trust Matrix" in reason


def test_unvetted_sender_is_filtered_out(db_session):
    """Ensure unvetted/unknown senders are filtered out before touching the pipeline."""
    unvetted_sender = "attacker@unknown-phishing.org"
    trusted, reason = is_source_trusted(unvetted_sender, db=db_session)
    assert trusted is False
    assert "not registered in Source Trust Matrix" in reason


def test_untrusted_email_staged_as_quarantined_and_blocked_from_pipeline(db_session):
    """Verify that an untrusted sender email is quarantined as UNTRUSTED_SOURCE and never enters the pipeline."""
    from app.config import get_settings
    settings = get_settings()

    stage_id = "STG-TEST-UNTRUSTED-01"
    res = _stage_and_verify_single_email(
        db=db_session,
        stage_id=stage_id,
        subject="Draft BL from unknown sender",
        body="Attaching draft BL.",
        sender_email="hacker@unvetted-domain.biz",
        raw_sender="hacker@unvetted-domain.biz",
        attachments=[{"filename": "test_BL.pdf", "bytes": b"%PDF-1.4 mock"}],
        source_mailbox="imap.inbound@averis.com",
        settings=settings,
    )

    staged = db_session.query(StagedEmailRecord).filter_by(stage_id=stage_id).first()
    assert staged is not None
    assert staged.status == "QUARANTINED"
    assert staged.security_status == "BLOCKED"
    assert staged.category == "UNTRUSTED_SOURCE"
    assert "Filtered" in staged.ai_reason

    # Verify pipeline was never touched (no EmailRecord created)
    ingested = db_session.query(EmailRecord).filter_by(email_id=f"INGEST-{stage_id}").first()
    assert ingested is None


def test_dynamically_added_source_becomes_trusted(db_session):
    """Verify that adding a source via GatewayPolicyRecord.source_policies immediately permits it."""
    new_shipper = "partner@global-freight-network.com"
    # Initially untrusted
    trusted_before, _ = is_source_trusted(new_shipper, db=db_session)
    assert trusted_before is False

    # Operator registers source in Trust Matrix
    pol = db_session.query(GatewayPolicyRecord).first()
    if not pol:
        pol = GatewayPolicyRecord(engine="rule", ingest_mode="auto")
        db_session.add(pol)
    current_policies = dict(pol.source_policies or {})
    current_policies[new_shipper] = "auto"
    pol.source_policies = current_policies
    db_session.commit()

    # Now trusted
    trusted_after, _ = is_source_trusted(new_shipper, db=db_session)
    assert trusted_after is True
