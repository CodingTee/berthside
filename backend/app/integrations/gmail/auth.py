"""OAuth helpers for the Gmail integration.

Secrets are never hard-coded. The Google OAuth client JSON and user token JSON
are read from paths configured in environment variables.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.config import get_settings

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]


def _state_path() -> Path:
    """Where the in-flight OAuth state (PKCE verifier) is kept.

    ``/api/gmail/connect`` and ``/api/gmail/oauth-callback`` are two separate
    HTTP requests, so a fresh ``Flow`` object is built in each of them. The
    Flow auto-generates a PKCE ``code_verifier`` when it builds the consent
    URL; if that verifier is not carried over to the callback, Google rejects
    the token exchange with ``invalid_grant`` and the user sees a bare
    "Internal Server Error". Persisting it next to the token keeps the two
    halves of the handshake in sync.
    """
    return Path(get_settings().gmail_token_file).parent / "gmail_oauth_state.json"


def _save_state(state: str, code_verifier: str | None) -> None:
    if not state:
        return
    path = _state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"state": state, "code_verifier": code_verifier}),
        encoding="utf-8",
    )


def _load_state() -> dict[str, Any]:
    path = _state_path()
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


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
    # Remember the PKCE verifier so the callback can finish the handshake.
    _save_state(state, getattr(flow, "code_verifier", None))
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
    # Restore the PKCE verifier created alongside the consent URL. Without it
    # Google answers invalid_grant and the callback blows up with a 500.
    saved = _load_state()
    if saved.get("code_verifier"):
        flow.code_verifier = saved["code_verifier"]
    try:
        flow.fetch_token(code=code)
    except Exception as exc:
        # Surface a readable reason instead of an opaque 500 page.
        raise RuntimeError(f"Google token exchange failed: {exc}") from exc
    creds = flow.credentials
    # The state file is intentionally left in place: it is overwritten by the
    # next /connect and holds nothing but a spent PKCE verifier. Deleting it
    # here is both unnecessary and fragile (some sandboxed runtimes refuse
    # filesystem deletions, which would abort the callback before the token is
    # ever written).

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
