"""Tests for Gmail REST API Outbound Dispatcher (HTTPS:443)."""
import io
import json
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError

from app.config import get_settings
from app.services.smtp_dispatcher import dispatch_smtp_email


def test_gmail_api_dispatch_success():
    """When GMAIL_REFRESH_TOKEN is configured, dispatcher sends via Gmail REST API (HTTPS)."""
    settings = get_settings()
    settings.gmail_client_id = "test_client_id_123"
    settings.gmail_client_secret = "test_client_secret_456"
    settings.gmail_refresh_token = "1//test_refresh_token_789"
    settings.gmail_demo_account = "averis.demo@gmail.com"
    settings.smtp_host = ""
    settings.resend_api_key = ""

    try:
        # Mock token refresh
        mock_token_resp = MagicMock()
        mock_token_resp.read.return_value = json.dumps({
            "access_token": "ya29.test_access_token_mock",
            "expires_in": 3600,
        }).encode("utf-8")
        mock_token_resp.__enter__.return_value = mock_token_resp

        # Mock send response
        mock_send_resp = MagicMock()
        mock_send_resp.read.return_value = json.dumps({
            "id": "1899abcdef123456",
            "threadId": "1899thread999999",
        }).encode("utf-8")
        mock_send_resp.__enter__.return_value = mock_send_resp

        def urlopen_side_effect(req, *args, **kwargs):
            if "oauth2.googleapis.com" in req.full_url:
                return mock_token_resp
            if "gmail.googleapis.com" in req.full_url:
                return mock_send_resp
            raise ValueError(f"Unexpected URL: {req.full_url}")

        with patch("urllib.request.urlopen", side_effect=urlopen_side_effect) as mock_urlopen:
            res = dispatch_smtp_email(
                to_email="client@partner-logistics.com",
                subject="Re: Bill of Lading BL-2026-001 Verification",
                body="Documents verified successfully. No discrepancies found.",
                html_body="<p>Documents verified successfully.</p>",
                in_reply_to="<orig-bl-111@partner-logistics.com>",
                references="<orig-bl-111@partner-logistics.com>",
                cc="operations@averis.com",
            )

            assert res["delivery"] == "SENT_SMTP"
            assert res["status"] == "SUCCESS"
            assert res["gmail_id"] == "1899abcdef123456"
            assert res["thread_id"] == "1899thread999999"
            assert res["channel"] == "Gmail REST API (HTTPS:443)"
            assert res["to"] == "client@partner-logistics.com"
            assert res["cc"] == "operations@averis.com"

            # Check that two HTTP requests were made: token refresh + message send
            assert mock_urlopen.call_count == 2
            token_call = mock_urlopen.call_args_list[0][0][0]
            send_call = mock_urlopen.call_args_list[1][0][0]

            assert "oauth2.googleapis.com" in token_call.full_url
            assert "gmail.googleapis.com" in send_call.full_url
            assert send_call.get_header("Authorization") == "Bearer ya29.test_access_token_mock"

            send_payload = json.loads(send_call.data.decode("utf-8"))
            assert "raw" in send_payload
    finally:
        settings.gmail_client_id = ""
        settings.gmail_client_secret = ""
        settings.gmail_refresh_token = ""


def test_gmail_api_failure_falls_back_to_resend_or_smtp():
    """When Gmail API encounters an error, it falls back to Resend or SMTP."""
    settings = get_settings()
    settings.gmail_client_id = "test_client_id"
    settings.gmail_client_secret = "test_client_secret"
    settings.gmail_refresh_token = "test_refresh_token"
    settings.resend_api_key = "re_test_backup_key"
    settings.resend_from = "Averis BerthSide Hub <onboarding@resend.dev>"
    settings.smtp_host = ""

    try:
        # Mock token error (e.g. invalid grant or 401)
        http_err = HTTPError(
            url="https://oauth2.googleapis.com/token",
            code=401,
            msg="Unauthorized",
            hdrs={},
            fp=io.BytesIO(b'{"error": "invalid_grant"}'),
        )

        mock_resend_resp = MagicMock()
        mock_resend_resp.read.return_value = json.dumps({"id": "resend_fallback_123"}).encode("utf-8")
        mock_resend_resp.__enter__.return_value = mock_resend_resp

        def urlopen_side_effect(req, *args, **kwargs):
            if "oauth2.googleapis.com" in req.full_url:
                raise http_err
            if "api.resend.com" in req.full_url:
                return mock_resend_resp
            raise ValueError(f"Unexpected URL: {req.full_url}")

        with patch("urllib.request.urlopen", side_effect=urlopen_side_effect):
            res = dispatch_smtp_email(
                to_email="client@example.com",
                subject="Test Fallback",
                body="Body",
            )

            assert res["delivery"] == "SENT_SMTP"
            assert res["status"] == "SUCCESS"
            assert res["channel"] == "Resend API (HTTPS)"
            assert res["message_id"] == "resend_fallback_123"
    finally:
        settings.gmail_client_id = ""
        settings.gmail_client_secret = ""
        settings.gmail_refresh_token = ""
        settings.resend_api_key = ""
