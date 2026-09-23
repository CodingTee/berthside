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
            assert res["polled_count"] == 2

            # Verify that 2 independent staged records were created with isolated attachments
            staged_records = (
                db_session.query(StagedEmailRecord)
                .filter_by(sender="operator@forwarder.com")
                .order_by(StagedEmailRecord.stage_id)
                .all()
            )
            assert len(staged_records) == 2
            stg1, stg2 = staged_records
            assert stg1.subject == "Original SI for BKG-8831"
            assert stg1.attachments == ["BKG-8831_SI.txt"]
            assert "-01" in stg1.stage_id

            assert stg2.subject == "Original Draft B/L for BKG-8831"
            assert stg2.attachments == ["BKG-8831_BL.txt"]
            assert "-02" in stg2.stage_id
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
    msg["From"] = "Spammer <spammer@example.com>"
    msg["To"] = "hub@example.com"
    msg["Subject"] = "I am SPAM - win free lottery click here"
    msg["Message-ID"] = "<spam-101@example.com>"
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

            staged = db_session.query(StagedEmailRecord).filter_by(sender="spammer@example.com").first()
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
    msg["From"] = "Operator <operator@averis.com>"
    msg["To"] = "hub@example.com"
    msg["Subject"] = "Fwd: BKG-9900 Draft BL for Verification"
    msg["Message-ID"] = "<fwd-101@gmail.com>"
    fwd_body = (
        "---------- Forwarded message ---------\n"
        "From: Alice Shipper <alice@shipper-corp.com>\n"
        "Subject: BKG-9900 Draft BL for Verification\n"
        "To: operator@averis.com\n\n"
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
            assert call_kwargs["to_email"] == "operator@averis.com"
            # Body must have mailto to alice
            assert "mailto:alice%40shipper-corp.com" in call_kwargs["body"] or "mailto:alice@shipper-corp.com" in call_kwargs["body"]
            # Body must have Gmail search with from: operator
            assert "from%3Aalice%40shipper-corp.com" in call_kwargs["body"]
            # HTML body must have action button and target client
            assert "Locate Exact Thread in Gmail" in call_kwargs.get("html_body", "")
            assert "alice@shipper-corp.com" in call_kwargs.get("html_body", "")
    finally:
        settings.imap_host = ""
        settings.imap_port = 993
        settings.imap_user = ""
        settings.imap_password = ""


def test_bridge_locate_and_copy_endpoint(client):
    """Verify that the Smart Bridge endpoint renders HTML with clipboard copy script and redirect target."""
    import base64

    target = "https://mail.google.com/mail/u/0/#search/from%3Aalice%40corp.com"
    text = "Dear Customer,\n\nVerified with 0 discrepancies."
    target_b64 = base64.urlsafe_b64encode(target.encode("utf-8")).decode("ascii")
    text_b64 = base64.urlsafe_b64encode(text.encode("utf-8")).decode("ascii")

    res = client.get(f"/api/v1/gateway/bridge/locate-and-copy?target={target_b64}&text={text_b64}")
    assert res.status_code == 200
    assert "text/html" in res.headers["content-type"]
    body = res.text
    assert "navigator.clipboard.writeText" in body
    assert "Verified with 0 discrepancies" in body
    assert "https://mail.google.com/mail/u/0/#search/from%3Aalice%40corp.com" in body


def test_imap_receipt_rendered_when_forwarded_from_same_address(db_session):
    """Verify that when sender_email == orig_client (e.g. user testing with their own forward),
    action dock, buttons, and enterprise audit receipt are FULLY rendered (never suppressed)."""
    settings = get_settings()
    settings.imap_host = "imap.example.com"
    settings.imap_port = 993
    settings.imap_user = "hub@example.com"
    settings.imap_password = "password"
    settings.auto_reply_on_verification = True

    msg = EmailMessage()
    msg["From"] = "Operator <operator@averis.com>"
    msg["To"] = "hub@example.com"
    msg["Subject"] = "Fwd: Draft BL BKG-1122 for Verification"
    msg["Message-ID"] = "<user-test-888@gmail.com>"
    fwd_body = (
        "---------- Forwarded message ---------\n"
        "From: operator@averis.com\n"
        "Subject: Draft BL BKG-1122 for Verification\n"
        "To: hub@example.com\n\n"
        "Please verify the attached draft BL."
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
            assert call_kwargs["to_email"] == "operator@averis.com"

            html = call_kwargs.get("html_body", "")
            text = call_kwargs.get("body", "")

            # Verify action dock and buttons are NOT suppressed
            assert "One-Click Reply to Client" in html
            assert "Official Customer Notice Card" in html or "Shipping Document Verification Notice" in html
            assert "Field-by-Field Counterpart Reconciliation Ledger" in html
            assert "Shipping Document Verification Receipt" in html

            # Verify plain text dock
            assert "Fast Client Reply Gateway" in text
            assert "SHIPMENT OVERVIEW" in text
    finally:
        settings.imap_host = ""
        settings.imap_port = 993
        settings.imap_user = ""
        settings.imap_password = ""


def test_customer_notice_endpoints(client, db_session):
    """Test customer notice preview page and 1-click dispatch API/web flow."""
    from app.models import StagedEmailRecord, ReportRecord, DispatchRecord
    from unittest.mock import patch

    # Seed staged and report records
    staged = StagedEmailRecord(
        stage_id="STG-NOTICE-007",
        source_mailbox="averis.demo@gmail.com",
        sender="operator-forwarder@test.com",
        recipient="averis.demo@gmail.com",
        subject="Fwd: Draft BL BKG-4455 for Verification",
        body="---------- Forwarded message ---------\nFrom: Alice <shipper@exportcorp.com>\nSubject: Draft BL\n\nPlease find draft BL attached.",
        attachments=["Draft_BL.pdf"],
        security_status="CLEAN",
        category="BL_COMPARISON",
        status="APPROVED",
    )
    db_session.add(staged)
    db_session.commit()

    rep = ReportRecord(
        email_id="INGEST-STG-NOTICE-007",
        status="OK",
        category="BL_COMPARISON",
        extracted={
            "si": {"booking_number": "BKG-4455", "vessel": "EVER CHIC", "voyage": "100W", "gross_weight_kg": 18000.0, "container_count": 4},
            "bl": {"booking_number": "BKG-4455", "vessel": "EVER CHIC", "voyage": "100W", "gross_weight_kg": 18000.0, "container_count": 4},
        },
        field_results=[
            {"field": "booking_number", "si_value": "BKG-4455", "bl_value": "BKG-4455", "match": True},
        ],
    )
    db_session.add(rep)
    db_session.commit()

    # 1. Test GET Preview Page
    res_preview = client.get("/api/v1/gateway/customer-notice/send?stage_id=STG-NOTICE-007")
    assert res_preview.status_code == 200
    assert "Confirm Official Customer Verification Notice" in res_preview.text
    assert "Live Customer Notice Email Preview" in res_preview.text
    assert "EVER CHIC / 100W" in res_preview.text
    assert "shipper@exportcorp.com" in res_preview.text

    # 2. Test 1-Click Auto Dispatch Flow
    with patch("app.services.smtp_dispatcher.dispatch_smtp_email") as mock_smtp:
        mock_smtp.return_value = {"delivery": "SENT_SMTP", "channel": "SMTP"}
        res_dispatch = client.get("/api/v1/gateway/customer-notice/send?stage_id=STG-NOTICE-007&auto_send=true")
        assert res_dispatch.status_code == 200
        assert "Official Customer Notice Dispatched Successfully" in res_dispatch.text
        assert "shipper@exportcorp.com" in res_dispatch.text

        mock_smtp.assert_called_once()
        kwargs = mock_smtp.call_args[1]
        assert kwargs["to_email"] == "shipper@exportcorp.com"
        assert kwargs["cc"] == "operator-forwarder@test.com"
        assert "EVER CHIC / 100W" in kwargs["html_body"]
        assert "VERIFIED WITH 0 DISCREPANCIES" in kwargs["html_body"]

        # Verify DispatchRecord saved
        rec = db_session.query(DispatchRecord).filter_by(stage_id="STG-NOTICE-007").first()
        assert rec is not None
        assert rec.recipient == "shipper@exportcorp.com"

        # 3. Test POST API Dispatch
    with patch("app.services.smtp_dispatcher.dispatch_smtp_email") as mock_smtp2:
        mock_smtp2.return_value = {"delivery": "SENT_SMTP", "channel": "SMTP"}
        res_api = client.post(
            "/api/v1/gateway/customer-notice/dispatch",
            json={"stage_id": "STG-NOTICE-007", "recipient": "custom_recipient@test.com"},
        )
        assert res_api.status_code == 200
        data = res_api.json()
        assert data["status"] == "SUCCESS"
        assert data["recipient"] == "custom_recipient@test.com"

    # 4. Test 1-Click Console Direct Dispatch (CC Operator)
    with patch("app.services.smtp_dispatcher.dispatch_smtp_email") as mock_smtp3:
        mock_smtp3.return_value = {"delivery": "SENT_SMTP", "channel": "SMTP"}
        res_console = client.post("/api/v1/gateway/emails/STG-NOTICE-007/dispatch-client")
        assert res_console.status_code == 200
        cdata = res_console.json()
        assert cdata["status"] == "SUCCESS"
        assert cdata["recipient"] == "shipper@exportcorp.com"
        assert cdata["cc"] == "operator-forwarder@test.com"

        mock_smtp3.assert_called_once()
        ckw = mock_smtp3.call_args[1]
        assert ckw["to_email"] == "shipper@exportcorp.com"
        assert ckw["cc"] == "operator-forwarder@test.com"
        assert "EVER CHIC / 100W" in ckw["html_body"]

        # Verify staged status flipped to RETURNED
        stg = db_session.query(StagedEmailRecord).filter_by(stage_id="STG-NOTICE-007").first()
        assert stg.status == "RETURNED"






