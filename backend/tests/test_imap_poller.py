from email.message import EmailMessage
from unittest.mock import MagicMock, patch
import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.main import app
from app.models import StagedEmailRecord
from app.services.imap_poller import poll_imap_inbox


@pytest.fixture
def client():
    return TestClient(app)


def test_imap_unconfigured_skips(db_session):
    """When IMAP host is not configured, polling safely returns SKIPPED status."""
    settings = get_settings()
    old_host = settings.imap_host
    settings.imap_host = ""
    try:
        res = poll_imap_inbox(db_session)
        assert res["status"] == "SKIPPED"
        assert res["polled_count"] == 0
    finally:
        settings.imap_host = old_host


def test_email_channel_status_endpoint(client):
    """Test GET /api/v1/gateway/email-channel/status."""
    res = client.get("/api/v1/gateway/email-channel/status")
    assert res.status_code == 200
    d = res.json()
    assert "smtp" in d
    assert "imap" in d
    assert d["smtp"]["mode"] in ("LIVE_SMTP", "SIMULATED")
    assert d["imap"]["mode"] in ("LIVE_IMAP", "SIMULATED_SANDBOX")


def test_imap_mocked_pull_and_stage(db_session):
    """Test IMAP fetching an unread email, staging it, and dispatching reply."""
    settings = get_settings()
    settings.imap_host = "imap.example.com"
    settings.imap_port = 993
    settings.imap_user = "hub@example.com"
    settings.imap_password = "password"
    settings.auto_reply_on_verification = True

    # Construct mock RFC822 email message
    msg = EmailMessage()
    msg["From"] = "John Doe <shipper@customercorp.com>"
    msg["To"] = "hub@example.com"
    msg["Subject"] = "Draft BL BKG-7719 for Verification"
    msg["Message-ID"] = "<msg-999@customercorp.com>"
    msg.set_content("Please find attached the draft B/L and SI.")
    msg.add_attachment(b"%PDF-1.4 Mock Draft BL Content", maintype="application", subtype="pdf", filename="Draft_BL.pdf")
    raw_bytes = msg.as_bytes()

    try:
        with patch("imaplib.IMAP4_SSL") as mock_imap_cls, \
             patch("app.services.imap_poller.dispatch_smtp_email") as mock_smtp:
            mock_mail = MagicMock()
            mock_imap_cls.return_value = mock_mail
            mock_mail.search.return_value = ("OK", [b"1"])
            mock_mail.fetch.return_value = ("OK", [(b"1 (RFC822 {100})", raw_bytes)])
            mock_smtp.return_value = {"delivery": "SENT_SMTP", "channel": "SMTP"}

            res = poll_imap_inbox(db_session)
            assert res["status"] == "SUCCESS"
            assert res["polled_count"] == 1

            # Verify staged record created in DB
            staged = db_session.query(StagedEmailRecord).filter_by(sender="shipper@customercorp.com").first()
            assert staged is not None
            assert staged.subject == "Draft BL BKG-7719 for Verification"
            assert "Draft_BL.pdf" in staged.attachments

            # Verify auto-reply was triggered
            mock_smtp.assert_called_once()
            call_kwargs = mock_smtp.call_args[1]
            assert call_kwargs["to_email"] == "shipper@customercorp.com"
            assert "Re: Draft BL BKG-7719 for Verification" in call_kwargs["subject"]
            assert call_kwargs["in_reply_to"] == "<msg-999@customercorp.com>"

            # Verify message was marked as read
            mock_mail.store.assert_called_once_with(b"1", "+FLAGS", "\\Seen")
    finally:
        settings.imap_host = ""
        settings.imap_port = 993
        settings.imap_user = ""
        settings.imap_password = ""


def test_imap_batch_forwarded_attachments_unpacked(db_session):
    """Verify Gmail 'Forward as attachment' bundling multiple .eml files is unpacked into a single record."""
    from email.mime.multipart import MIMEMultipart
    from email.mime.text import MIMEText
    from email.mime.application import MIMEApplication
    from email.mime.message import MIMEMessage

    settings = get_settings()
    settings.imap_host = "imap.example.com"
    settings.imap_port = 993
    settings.imap_user = "hub@example.com"
    settings.imap_password = "password"
    settings.auto_reply_on_verification = True

    # Inner Email 1: Shipping Instruction
    inner1 = MIMEMultipart()
    inner1["Subject"] = "Original SI for BKG-8831"
    inner1.attach(MIMEText("Please process the attached SI."))
    si_content = (
        b"SHIPPING INSTRUCTION\n"
        b"Booking Number: BKG-8831\n"
        b"Shipper: Acme Logistics Ltd\n"
        b"Consignee: Global Import Corp\n"
        b"Notify Party: Global Import Corp\n"
        b"Vessel: EVER GIVEN\n"
        b"Voyage: 042E\n"
        b"Port of Loading: SHANGHAI\n"
        b"Port of Discharge: ROTTERDAM\n"
        b"Containers: 5x40HC\n"
        b"Gross Weight: 12000.0 KGS\n"
    )
    si_att = MIMEApplication(si_content, "octet-stream")
    si_att.add_header("Content-Disposition", "attachment", filename="BKG-8831_SI.txt")
    inner1.attach(si_att)

    # Inner Email 2: Bill of Lading
    inner2 = MIMEMultipart()
    inner2["Subject"] = "Original Draft B/L for BKG-8831"
    inner2.attach(MIMEText("Please find draft BL."))
    bl_content = (
        b"BILL OF LADING\n"
        b"Booking Number: BKG-8831\n"
        b"Shipper: Acme Logistics Ltd\n"
        b"Consignee: Global Import Corp\n"
        b"Notify Party: Global Import Corp\n"
        b"Vessel: EVER GIVEN\n"
        b"Voyage: 042E\n"
        b"Port of Loading: SHANGHAI\n"
        b"Port of Discharge: ROTTERDAM\n"
        b"Containers: 5x40HC\n"
        b"Gross Weight: 12000.0 KGS\n"
    )
    bl_att = MIMEApplication(bl_content, "octet-stream")
    bl_att.add_header("Content-Disposition", "attachment", filename="BKG-8831_BL.txt")
    inner2.attach(bl_att)

    # Outer Email: Batch Forward as Attachment from Gmail
    outer = MIMEMultipart()
    outer["From"] = "Operator <operator@forwarder.com>"
    outer["To"] = "hub@example.com"
    outer["Subject"] = "FWD: Batch verification for BKG-8831"
    outer["Message-ID"] = "<batch-fwd-101@forwarder.com>"
    outer.attach(MIMEText("Forwarding both SI and BL emails for cross-audit."))

    fwd1 = MIMEMessage(inner1)
    fwd1.add_header("Content-Disposition", "attachment", filename="email1_si.eml")
    outer.attach(fwd1)

    fwd2 = MIMEMessage(inner2)
    fwd2.add_header("Content-Disposition", "attachment", filename="email2_bl.eml")
    outer.attach(fwd2)

    raw_bytes = outer.as_bytes()

    try:
        with patch("imaplib.IMAP4_SSL") as mock_imap_cls, \
             patch("app.services.imap_poller.dispatch_smtp_email") as mock_smtp:
            mock_mail = MagicMock()
            mock_imap_cls.return_value = mock_mail
            mock_mail.search.return_value = ("OK", [b"1"])
            mock_mail.fetch.return_value = ("OK", [(b"1 (RFC822 {200})", raw_bytes)])
            mock_smtp.return_value = {"delivery": "SENT_SMTP", "channel": "SMTP"}

            res = poll_imap_inbox(db_session)
            assert res["status"] == "SUCCESS"
            assert res["polled_count"] == 1

            # Verify that single staged record contains attachments from BOTH inner emails
            staged = db_session.query(StagedEmailRecord).filter_by(sender="operator@forwarder.com").first()
            assert staged is not None
            assert staged.subject == "FWD: Batch verification for BKG-8831"
            assert "BKG-8831_SI.txt" in staged.attachments
            assert "BKG-8831_BL.txt" in staged.attachments

            # Verify that workflow processed the single email pairing SI and BL from both forwarded emails
            from app.models import ReportRecord
            report = db_session.query(ReportRecord).filter_by(email_id=f"INGEST-{staged.stage_id}").first()
            assert report is not None
            assert report.status in ("OK", "MISMATCH")
            assert report.category == "BL_COMPARISON"

            # Verify unified reply sent back to sender
            mock_smtp.assert_called_once()
            call_kwargs = mock_smtp.call_args[1]
            assert call_kwargs["to_email"] == "operator@forwarder.com"
            assert "Verdict: OK" in call_kwargs["body"] or "Verdict: MISMATCH" in call_kwargs["body"]
    finally:
        settings.imap_host = ""
        settings.imap_port = 993
        settings.imap_user = ""
        settings.imap_password = ""


def test_imap_spam_silent_drop_no_reply(db_session):
    """Verify that spam emails (e.g. 'I am SPAM') are quarantined and NEVER trigger auto-reply."""
    settings = get_settings()
    settings.imap_host = "imap.example.com"
    settings.imap_port = 993
    settings.imap_user = "hub@example.com"
    settings.imap_password = "password"
    settings.auto_reply_on_verification = True

    msg = EmailMessage()
    msg["From"] = "Spammer <spammer@junkmailer.com>"
    msg["To"] = "hub@example.com"
    msg["Subject"] = "I am SPAM - win free lottery click here"
    msg["Message-ID"] = "<spam-101@junkmailer.com>"
    msg.set_content("Congratulations you won a lottery prize!")
    raw_bytes = msg.as_bytes()

    try:
        with patch("imaplib.IMAP4_SSL") as mock_imap_cls, \
             patch("app.services.imap_poller.dispatch_smtp_email") as mock_smtp:
            mock_mail = MagicMock()
            mock_imap_cls.return_value = mock_mail
            mock_mail.search.return_value = ("OK", [b"1"])
            mock_mail.fetch.return_value = ("OK", [(b"1 (RFC822 {100})", raw_bytes)])

            res = poll_imap_inbox(db_session)
            assert res["status"] == "SUCCESS"
            assert res["polled_count"] == 1

            staged = db_session.query(StagedEmailRecord).filter_by(sender="spammer@junkmailer.com").first()
            assert staged is not None
            assert staged.category == "SPAM"
            assert staged.status == "QUARANTINED"

            # STRICT VERIFICATION: NO auto-reply sent! Silent drop!
            mock_smtp.assert_not_called()
    finally:
        settings.imap_host = ""
        settings.imap_port = 993
        settings.imap_user = ""
        settings.imap_password = ""


def test_imap_malware_silent_drop_no_reply(db_session):
    """Verify that dangerous executable payloads are blocked and NEVER trigger auto-reply."""
    settings = get_settings()
    settings.imap_host = "imap.example.com"
    settings.imap_port = 993
    settings.imap_user = "hub@example.com"
    settings.imap_password = "password"
    settings.auto_reply_on_verification = True

    msg = EmailMessage()
    msg["From"] = "Attacker <attacker@evil.org>"
    msg["To"] = "hub@example.com"
    msg["Subject"] = "Urgent Bill of Lading Document"
    msg["Message-ID"] = "<malware-666@evil.org>"
    msg.set_content("Please run the attached shipping doc updater.")
    msg.add_attachment(b"MZ\x90\x00\x03fake-executable-bytes", maintype="application", subtype="octet-stream", filename="B_L_Update.exe")
    raw_bytes = msg.as_bytes()

    try:
        with patch("imaplib.IMAP4_SSL") as mock_imap_cls, \
             patch("app.services.imap_poller.dispatch_smtp_email") as mock_smtp:
            mock_mail = MagicMock()
            mock_imap_cls.return_value = mock_mail
            mock_mail.search.return_value = ("OK", [b"1"])
            mock_mail.fetch.return_value = ("OK", [(b"1 (RFC822 {100})", raw_bytes)])

            res = poll_imap_inbox(db_session)
            assert res["status"] == "SUCCESS"
            assert res["polled_count"] == 1

            staged = db_session.query(StagedEmailRecord).filter_by(sender="attacker@evil.org").first()
            assert staged is not None
            assert staged.security_status == "BLOCKED"
            assert staged.status == "QUARANTINED"

            # STRICT VERIFICATION: NO auto-reply sent! Zero backscatter!
            mock_smtp.assert_not_called()
    finally:
        settings.imap_host = ""
        settings.imap_port = 993
        settings.imap_user = ""
        settings.imap_password = ""


def test_imap_forwarded_receipt_includes_action_links(db_session):
    """Verify that when an operator forwards a customer's email, the receipt contains 1-click mailto and Gmail from: search links."""
    settings = get_settings()
    settings.imap_host = "imap.example.com"
    settings.imap_port = 993
    settings.imap_user = "hub@example.com"
    settings.imap_password = "password"
    settings.auto_reply_on_verification = True

    msg = EmailMessage()
    msg["From"] = "Operator <testuse1491@gmail.com>"
    msg["To"] = "hub@example.com"
    msg["Subject"] = "Fwd: BKG-9900 Draft BL for Verification"
    msg["Message-ID"] = "<fwd-101@gmail.com>"
    fwd_body = (
        "---------- Forwarded message ---------\n"
        "From: Alice Shipper <alice@shipper-corp.com>\n"
        "Subject: BKG-9900 Draft BL for Verification\n"
        "To: testuse1491@gmail.com\n\n"
        "Attached draft documents."
    )
    msg.set_content(fwd_body)
    msg.add_attachment(b"%PDF-1.4 Mock Draft BL Content", maintype="application", subtype="pdf", filename="Draft_BL.pdf")
    raw_bytes = msg.as_bytes()

    try:
        with patch("imaplib.IMAP4_SSL") as mock_imap_cls, \
             patch("app.services.imap_poller.dispatch_smtp_email") as mock_smtp:
            mock_mail = MagicMock()
            mock_imap_cls.return_value = mock_mail
            mock_mail.search.return_value = ("OK", [b"1"])
            mock_mail.fetch.return_value = ("OK", [(b"1 (RFC822 {100})", raw_bytes)])
            mock_smtp.return_value = {"delivery": "SENT_SMTP", "channel": "SMTP"}

            res = poll_imap_inbox(db_session)
            assert res["status"] == "SUCCESS"
            assert res["polled_count"] == 1

            mock_smtp.assert_called_once()
            call_kwargs = mock_smtp.call_args[1]
            # Must reply to the operator
            assert call_kwargs["to_email"] == "testuse1491@gmail.com"
            # Body must have mailto to alice
            assert "mailto:alice%40shipper-corp.com" in call_kwargs["body"] or "mailto:alice@shipper-corp.com" in call_kwargs["body"]
            # Body must have Gmail search with from: operator
            assert "from%3Aalice%40shipper-corp.com" in call_kwargs["body"]
            # HTML body must have action button
            assert "from:alice@shipper-corp.com" in call_kwargs.get("html_body", "")
    finally:
        settings.imap_host = ""
        settings.imap_port = 993
        settings.imap_user = ""
        settings.imap_password = ""



