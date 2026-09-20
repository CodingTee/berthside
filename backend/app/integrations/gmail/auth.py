"""OAuth helpers for the Gmail integration.

Secrets are never hard-coded. The Google OAuth client JSON and user token JSON
are read from paths configured in environment variables.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from app.config import get_settings

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]


def oauth_available() -> bool:
    """True when the optional Google client libraries are importable."""
    try:
        import google.auth.transport.requests  # noqa: F401
        import google.oauth2.credentials  # noqa: F401
        import google_auth_oauthlib.flow  # noqa: F401
        return True
    except Exception:
        return False


def credentials_file_exists() -> bool:
    return Path(get_settings().gmail_credentials_file).is_file()


def token_file_exists() -> bool:
    return Path(get_settings().gmail_token_file).is_file()


def integration_status() -> dict[str, Any]:
    settings = get_settings()
    return {
        "account": settings.gmail_demo_account,
        "oauth_libraries_available": oauth_available(),
        "credentials_configured": credentials_file_exists(),
        "token_configured": token_file_exists(),
        "polling_enabled": settings.gmail_polling_enabled,
        "query": settings.gmail_query,
    }


def build_authorization_url() -> dict[str, str]:
    """Create the Google OAuth consent URL for the dedicated demo mailbox."""
    if not oauth_available():
        raise RuntimeError("Google OAuth libraries are not installed")
    if not credentials_file_exists():
        raise FileNotFoundError("Gmail OAuth client JSON not configured")

    from google_auth_oauthlib.flow import Flow

    settings = get_settings()
    flow = Flow.from_client_secrets_file(
        settings.gmail_credentials_file,
        scopes=SCOPES,
        redirect_uri=settings.gmail_oauth_redirect_uri,
    )
    auth_url, state = flow.authorization_url(
        access_type="offline",
        include_granted_scopes="true",
        prompt="consent",
        login_hint=settings.gmail_demo_account,
    )
    return {"authorization_url": auth_url, "state": state}


def exchange_code_for_token(code: str) -> dict[str, str]:
    """Exchange a Google OAuth code and persist the resulting token JSON."""
    if not oauth_available():
        raise RuntimeError("Google OAuth libraries are not installed")
    if not credentials_file_exists():
        raise FileNotFoundError("Gmail OAuth client JSON not configured")

    from google_auth_oauthlib.flow import Flow

    settings = get_settings()
    flow = Flow.from_client_secrets_file(
        settings.gmail_credentials_file,
        scopes=SCOPES,
        redirect_uri=settings.gmail_oauth_redirect_uri,
    )
    flow.fetch_token(code=code)
    creds = flow.credentials

    token_path = Path(settings.gmail_token_file)
    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.write_text(creds.to_json(), encoding="utf-8")
    return {
        "status": "connected",
        "account": settings.gmail_demo_account,
        "token_file": str(token_path),
    }


def load_credentials():
    """Load and refresh OAuth credentials if needed."""
    if not oauth_available():
        raise RuntimeError("Google OAuth libraries are not installed")
    if not token_file_exists():
        raise FileNotFoundError("Gmail token JSON not configured")

    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    settings = get_settings()
    creds = Credentials.from_authorized_user_file(settings.gmail_token_file, SCOPES)
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        Path(settings.gmail_token_file).write_text(creds.to_json(), encoding="utf-8")
    if not creds.valid:
        raise RuntimeError("Gmail OAuth token is invalid; reconnect Gmail")
    return creds
