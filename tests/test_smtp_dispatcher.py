"""Tests for Outbound SMTP Dispatcher and Thread Preservation."""
from unittest.mock import MagicMock, patch

from app.config import get_settings
from app.services.smtp_dispatcher import dispatch_smtp_email


def test_smtp_unconfigured_fallback():
    """When no SMTP server is configured, dispatch_smtp_email defaults safely to SIMULATED."""
    settings = get_settings()
    old_host = settings.smtp_host
    settings.smtp_host = ""
    try:
        res = dispatch_smtp_email(
            to_email="shipper@fastocean.com",
            subject="Test Fallback",
            body="Body content",
        )
        assert res["delivery"] == "SIMULATED"
        assert res["status"] == "FALLBACK_SIMULATED"
        assert res["to"] == "shipper@fastocean.com"
        assert res["subject"] == "Test Fallback"
        assert "message_id" in res
    finally:
        settings.smtp_host = old_host


def test_smtp_mocked_live_dispatch():
    """When SMTP credentials are provided, email is formatted with RFC headers and sent."""
    settings = get_settings()
    settings.smtp_host = "smtp.example.com"
    settings.smtp_port = 587
    settings.smtp_user = "sdoc@example.com"
    settings.smtp_password = "secretpassword"
    settings.smtp_from = "sdoc@example.com"
    settings.smtp_use_tls = True

    try:
        with patch("smtplib.SMTP") as mock_smtp_cls:
            mock_server = MagicMock()
            mock_smtp_cls.return_value = mock_server

            res = dispatch_smtp_email(
                to_email="customer@private-domain.com",
                subject="Re: Shipping Instruction SI-9921 - Verified",
                body="Discrepancies found: 0",
                in_reply_to="<orig-12345@private-domain.com>",
                references="<orig-12345@private-domain.com>",
            )

            assert res["delivery"] == "SENT_SMTP"
            assert res["status"] == "SUCCESS"
            assert res["to"] == "customer@private-domain.com"

            # Verify SMTP interactions
            mock_smtp_cls.assert_called_once_with("smtp.example.com", 587, timeout=15)
            assert mock_server.starttls.called
            mock_server.login.assert_called_once_with("sdoc@example.com", "secretpassword")
            assert mock_server.send_message.called
            sent_msg = mock_server.send_message.call_args[0][0]
            assert sent_msg["To"] == "customer@private-domain.com"
            assert sent_msg["Subject"] == "Re: Shipping Instruction SI-9921 - Verified"
            assert sent_msg["In-Reply-To"] == "<orig-12345@private-domain.com>"
            assert sent_msg["References"] == "<orig-12345@private-domain.com>"
    finally:
        # Reset settings
        settings.smtp_host = ""
        settings.smtp_port = 587
        settings.smtp_user = ""
        settings.smtp_password = ""
        settings.smtp_from = ""


def test_smtp_connection_failure_fails_gracefully():
    """When SMTP connection fails, it catches the error and falls back without crashing."""
    settings = get_settings()
    settings.smtp_host = "smtp.bad-server-address.invalid"
    settings.smtp_port = 587

    try:
        with patch("smtplib.SMTP", side_effect=ConnectionRefusedError("Connection refused")):
            res = dispatch_smtp_email(
                to_email="customer@private-domain.com",
                subject="Test Fail",
                body="Test",
            )
            assert res["delivery"] == "SIMULATED"
            assert res["status"] == "ERROR"
            assert "Connection refused" in res["error"]
    finally:
        settings.smtp_host = ""


def test_resend_api_dispatch_success():
    """When resend_api_key is set, dispatcher sends via Resend REST API (HTTPS)."""
    import io
    import json
    settings = get_settings()
    settings.resend_api_key = "re_test_secret_12345"
    settings.resend_from = "Averis BerthSide Hub <onboarding@resend.dev>"
    settings.smtp_host = ""

    try:
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({"id": "resend_msg_98765"}).encode("utf-8")
        mock_resp.__enter__.return_value = mock_resp

        with patch("urllib.request.urlopen", return_value=mock_resp) as mock_urlopen:
            res = dispatch_smtp_email(
                to_email="client@example.com",
                subject="Re: Official Maritime Audit Notice",
                body="Verification PASSED with 0 errors.",
                html_body="<b>Verification PASSED</b>",
                in_reply_to="<msg-orig-111@example.com>",
                references="<msg-orig-111@example.com>",
                cc="operator@example.com",
            )

            assert res["delivery"] == "SENT_SMTP"
            assert res["status"] == "SUCCESS"
            assert res["message_id"] == "resend_msg_98765"
            assert res["channel"] == "Resend API (HTTPS)"
            assert res["to"] == "client@example.com"
            assert res["cc"] == "operator@example.com"

            assert mock_urlopen.called
            req = mock_urlopen.call_args[0][0]
            assert req.full_url == "https://api.resend.com/emails"
            assert req.get_header("Authorization") == "Bearer re_test_secret_12345"
            assert req.get_header("Content-type") == "application/json"
            sent_payload = json.loads(req.data.decode("utf-8"))
            assert sent_payload["from"] == "Averis BerthSide Hub <onboarding@resend.dev>"
            assert sent_payload["to"] == ["client@example.com"]
            assert sent_payload["cc"] == ["operator@example.com"]
            assert sent_payload["headers"]["In-Reply-To"] == "<msg-orig-111@example.com>"
            assert sent_payload["headers"]["References"] == "<msg-orig-111@example.com>"
    finally:
        settings.resend_api_key = ""
        settings.resend_from = "Averis BerthSide Hub <onboarding@resend.dev>"


def test_resend_api_dispatch_error_fallback_simulated():
    """When Resend API returns HTTP error and no SMTP is configured, it falls back to SIMULATED ERROR."""
    import io
    from urllib.error import HTTPError
    settings = get_settings()
    settings.resend_api_key = "re_invalid_key"
    settings.smtp_host = ""

    try:
        http_err = HTTPError(
            url="https://api.resend.com/emails",
            code=401,
            msg="Unauthorized",
            hdrs={},
            fp=io.BytesIO(b'{"message": "API key invalid", "statusCode": 401}'),
        )
        with patch("urllib.request.urlopen", side_effect=http_err):
            res = dispatch_smtp_email(
                to_email="client@example.com",
                subject="Test Resend Error",
                body="Test",
            )
            assert res["delivery"] == "SIMULATED"
            assert res["status"] == "ERROR"
            assert "401" in res["error"]
            assert "Resend" in res["channel"]
    finally:
        settings.resend_api_key = ""


def test_resend_api_dispatch_error_fallback_to_smtp():
    """When Resend API fails but live SMTP is configured, it falls back to SMTP."""
    settings = get_settings()
    settings.resend_api_key = "re_failing_key"
    settings.smtp_host = "smtp.example.com"
    settings.smtp_port = 587
    settings.smtp_user = "user@example.com"
    settings.smtp_password = "password"

    try:
        with patch("urllib.request.urlopen", side_effect=Exception("Resend API unreachable")), \
             patch("smtplib.SMTP") as mock_smtp_cls:
            mock_server = MagicMock()
            mock_smtp_cls.return_value = mock_server

            res = dispatch_smtp_email(
                to_email="client@example.com",
                subject="Test Resend Fallback",
                body="Test",
            )
            assert res["delivery"] == "SENT_SMTP"
            assert res["status"] == "SUCCESS"
            assert "SMTP" in res["channel"]
            assert mock_server.send_message.called
    finally:
        settings.resend_api_key = ""
        settings.smtp_host = ""
        settings.smtp_port = 587
        settings.smtp_user = ""
        settings.smtp_password = ""
