"""OAuth 2.0 with PKCE helpers for Microsoft Outlook / Graph API (Personal Accounts supported)."""
from __future__ import annotations

import base64
import hashlib
import http.server
import json
import secrets
import threading
import time
import urllib.parse
from pathlib import Path
from typing import Any

import httpx

import os
from app.config import get_settings

# Pre-registered public client ID for Microsoft Personal / Consumer Accounts with PKCE
DEFAULT_CLIENT_ID = "9e5f94bc-e8a4-4e73-b8be-63364c29d753"
DEFAULT_REDIRECT_URI = "http://localhost:8400"
SCOPES = [
    "offline_access",
    "https://graph.microsoft.com/User.Read",
    "https://graph.microsoft.com/Mail.Read",
]

def _client_config_path() -> Path:
    settings = get_settings()
    base = Path(settings.gmail_token_file).parent
    return base / "outlook_client.json"

def get_client_config() -> dict[str, Any]:
    path = _client_config_path()
    if path.is_file():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}

def save_client_config(client_id: str, client_secret: str | None = None, redirect_uri: str | None = None) -> dict[str, Any]:
    path = _client_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    cfg = get_client_config()
    cfg["client_id"] = client_id.strip()
    if client_secret is not None:
        cfg["client_secret"] = client_secret.strip()
    if redirect_uri is not None:
        cfg["redirect_uri"] = redirect_uri.strip()
    path.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    return cfg

def get_effective_client_id() -> str:
    cfg = get_client_config()
    cid = cfg.get("client_id")
    if cid and cid.strip():
        return cid.strip()
    return os.environ.get("OUTLOOK_CLIENT_ID") or DEFAULT_CLIENT_ID

def get_effective_redirect_uri() -> str:
    cfg = get_client_config()
    r = cfg.get("redirect_uri")
    if r and r.strip():
        return r.strip()
    return DEFAULT_REDIRECT_URI

def get_auth_endpoint() -> str:
    cfg = get_client_config()
    tenant = cfg.get("tenant") or "consumers"
    return f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/authorize"

def get_token_endpoint() -> str:
    cfg = get_client_config()
    tenant = cfg.get("tenant") or "consumers"
    return f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token"

RESULT_PAGE_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>{title}</title>
  <style>
    body {{
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
      display: flex; align-items: center; justify-content: center;
      height: 100vh; margin: 0; background: #0f172a; color: #f8fafc; text-align: center;
    }}
    .card {{
      background: #1e293b; padding: 36px 48px; border-radius: 16px;
      box-shadow: 0 10px 25px rgba(0,0,0,0.5); max-width: 420px; border: 1px solid #334155;
    }}
    .icon {{ font-size: 48px; margin-bottom: 16px; }}
    h1 {{ font-size: 20px; margin: 0 0 12px; color: {tone}; }}
    p {{ font-size: 14px; line-height: 1.5; color: #94a3b8; margin: 0 0 20px; }}
    .btn {{
      background: #2563eb; color: #fff; border: none; padding: 10px 20px;
      border-radius: 8px; cursor: pointer; font-weight: 500; font-size: 14px;
    }}
  </style>
</head>
<body>
  <div class="card">
    <div class="icon">{icon}</div>
    <h1>{title}</h1>
    <p>{body}</p>
    <button class="btn" onclick="window.close()">Close this window</button>
  </div>
  <script>
    try {{
      localStorage.setItem('sdoc-outlook-connected', Date.now().toString());
      if (window.opener) {{
        window.opener.postMessage({{ type: 'outlook-connected' }}, '*');
      }}
    }} catch(e) {{}}
    setTimeout(() => window.close(), 2500);
  </script>
</body>
</html>"""


class _LocalCallbackHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        query = urllib.parse.urlparse(self.path).query
        params = urllib.parse.parse_qs(query)
        code = params.get("code", [None])[0]
        state = params.get("state", [None])[0]
        error = params.get("error", [None])[0]
        error_desc = params.get("error_description", [None])[0]

        if error:
            html = RESULT_PAGE_HTML.format(
                title="Outlook Authorization Failed",
                body=f"Microsoft returned: <code>{error_desc or error}</code>",
                tone="#ef4444",
                icon="❌",
            )
            code_num = 400
        elif not code:
            html = RESULT_PAGE_HTML.format(
                title="Authorization Incomplete",
                body="No authorization code was returned by Microsoft.",
                tone="#f59e0b",
                icon="⚠️",
            )
            code_num = 400
        else:
            try:
                res = exchange_code_for_token(code=code, state=state)
                acc = res.get("account", "Outlook")
                html = RESULT_PAGE_HTML.format(
                    title="ShipMail Connected to Outlook!",
                    body=f"Successfully linked personal account: <b>{acc}</b><br>You can close this tab now.",
                    tone="#10b981",
                    icon="✅",
                )
                code_num = 200
            except Exception as e:
                html = RESULT_PAGE_HTML.format(
                    title="Token Exchange Failed",
                    body=f"Could not complete handshake: {e}",
                    tone="#ef4444",
                    icon="❌",
                )
                code_num = 500

        self.send_response(code_num)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(html.encode("utf-8"))

    def log_message(self, format, *args):
        pass


_server_started = False
_server_lock = threading.Lock()


def ensure_callback_listener() -> None:
    global _server_started
    with _server_lock:
        if not _server_started:
            try:
                srv = http.server.HTTPServer(("127.0.0.1", 8400), _LocalCallbackHandler)
                t = threading.Thread(target=srv.serve_forever, daemon=True)
                t.start()
                _server_started = True
            except Exception:
                pass


def _token_path() -> Path:
    settings = get_settings()
    base = Path(settings.gmail_token_file).parent
    return base / "outlook_token.json"


def _state_path() -> Path:
    return _token_path().parent / "outlook_oauth_state.json"


def _save_state(state: str, code_verifier: str, redirect_uri: str) -> None:
    path = _state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"state": state, "code_verifier": code_verifier, "redirect_uri": redirect_uri}),
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


def token_file_exists() -> bool:
    return _token_path().is_file()


def get_stored_token() -> dict[str, Any] | None:
    p = _token_path()
    if not p.is_file():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def integration_status() -> dict[str, Any]:
    tok = get_stored_token()
    account = tok.get("account") if tok else None
    client_id = get_effective_client_id()
    redirect_uri = get_effective_redirect_uri()
    return {
        "account": account,
        "connected": bool(tok and tok.get("access_token")),
        "token_configured": bool(tok and tok.get("access_token")),
        "client_id": client_id,
        "client_id_configured": bool(client_id),
        "redirect_uri": redirect_uri,
    }


def build_authorization_url(login_hint: str | None = None) -> dict[str, str]:
    """Generate Microsoft OAuth consent URL supporting personal accounts."""
    client_id = get_effective_client_id()
    if not client_id:
        raise RuntimeError("Microsoft Application (client) ID is not configured. Please configure your Client ID first.")

    redirect_uri = get_effective_redirect_uri()
    if "8400" in redirect_uri:
        ensure_callback_listener()

    code_verifier = secrets.token_urlsafe(64)
    hashed = hashlib.sha256(code_verifier.encode("ascii")).digest()
    code_challenge = base64.urlsafe_b64encode(hashed).decode("ascii").rstrip("=")

    state = secrets.token_urlsafe(16)
    _save_state(state, code_verifier, redirect_uri)

    params = {
        "client_id": client_id,
        "response_type": "code",
        "redirect_uri": redirect_uri,
        "response_mode": "query",
        "scope": " ".join(SCOPES),
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
        "prompt": "select_account",
    }
    if login_hint:
        params["login_hint"] = login_hint

    auth_url = f"{get_auth_endpoint()}?{urllib.parse.urlencode(params)}"
    return {"authorization_url": auth_url, "state": state}


def exchange_code_for_token(code: str, state: str | None = None) -> dict[str, Any]:
    """Exchange code for access & refresh tokens using PKCE verifier."""
    saved = _load_state()
    code_verifier = saved.get("code_verifier")
    if not code_verifier:
        raise RuntimeError("No PKCE code verifier found in session state.")

    client_id = get_effective_client_id()
    redirect_uri = saved.get("redirect_uri") or get_effective_redirect_uri()
    cfg = get_client_config()

    data = {
        "client_id": client_id,
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
        "code_verifier": code_verifier,
    }
    if cfg.get("client_secret"):
        data["client_secret"] = cfg["client_secret"]

    with httpx.Client(timeout=15.0) as client:
        res = client.post(get_token_endpoint(), data=data)
        if res.status_code != 200:
            raise RuntimeError(f"Microsoft token exchange failed: {res.text}")
        token_data = res.json()

    # Get user profile to determine email address
    account_email = "Outlook Account"
    try:
        with httpx.Client(timeout=10.0) as client:
            me_res = client.get(
                "https://graph.microsoft.com/v1.0/me",
                headers={"Authorization": f"Bearer {token_data['access_token']}"},
            )
            if me_res.status_code == 200:
                me = me_res.json()
                account_email = me.get("mail") or me.get("userPrincipalName") or account_email
    except Exception:
        pass

    token_payload = {
        "access_token": token_data.get("access_token"),
        "refresh_token": token_data.get("refresh_token"),
        "expires_in": token_data.get("expires_in"),
        "expires_at": time.time() + float(token_data.get("expires_in", 3600)),
        "scope": token_data.get("scope"),
        "token_type": token_data.get("token_type"),
        "account": account_email,
    }

    p = _token_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(token_payload, indent=2), encoding="utf-8")

    return {
        "status": "connected",
        "account": account_email,
        "token_file": str(p),
    }


def get_valid_access_token() -> str:
    """Return a valid access token, auto-refreshing via refresh_token if expired."""
    tok = get_stored_token()
    if not tok or not tok.get("access_token"):
        raise RuntimeError("Outlook is not connected. Please connect Outlook first.")

    now = time.time()
    expires_at = tok.get("expires_at", 0)
    if now < expires_at - 300:
        return tok["access_token"]

    refresh_token = tok.get("refresh_token")
    if not refresh_token:
        return tok["access_token"]

    client_id = get_effective_client_id()
    cfg = get_client_config()
    data = {
        "client_id": client_id,
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "scope": " ".join(SCOPES),
    }
    if cfg.get("client_secret"):
        data["client_secret"] = cfg["client_secret"]

    with httpx.Client(timeout=15.0) as client:
        res = client.post(get_token_endpoint(), data=data)
        if res.status_code != 200:
            return tok["access_token"]
        new_data = res.json()

    tok["access_token"] = new_data.get("access_token", tok["access_token"])
    if "refresh_token" in new_data:
        tok["refresh_token"] = new_data["refresh_token"]
    tok["expires_in"] = new_data.get("expires_in", 3600)
    tok["expires_at"] = time.time() + float(tok["expires_in"])

    _token_path().write_text(json.dumps(tok, indent=2), encoding="utf-8")
    return tok["access_token"]
