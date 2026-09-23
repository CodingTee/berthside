"""Helper script to authorize Gmail API with both read and send permissions.

Generates a fresh refresh_token with 'gmail.send' and 'gmail.readonly' scopes
that works seamlessly over HTTPS:443 on Render.com (bypassing SMTP port blocking).
"""
import json
import os
import sys
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

BACKEND_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND_DIR))

CLIENT_SECRETS_FILE = BACKEND_DIR / "secrets" / "google_oauth_client.json"
TOKEN_FILE = BACKEND_DIR / "secrets" / "gmail_token.json"
SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
]


class OAuthCallbackHandler(BaseHTTPRequestHandler):
    auth_code = None

    def do_GET(self):
        query = parse_qs(urlparse(self.path).query)
        if "code" in query:
            OAuthCallbackHandler.auth_code = query["code"][0]
            self.send_response(200)
            self.send_header("Content-type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"""
            <html><body style="font-family:sans-serif;text-align:center;padding:50px;background:#0f172a;color:#f8fafc">
            <h1 style="color:#10b981">&#10004; Authorization Successful!</h1>
            <p>You can close this tab and return to your terminal.</p>
            </body></html>
            """)
        else:
            self.send_response(400)
            self.end_headers()
            self.wfile.write(b"No code found in request")

    def log_message(self, format, *args):
        pass  # Quiet logging


def main():
    print("=" * 60)
    print("  Gmail REST API Outbound Authorization (HTTPS:443)")
    print("=" * 60)

    if not CLIENT_SECRETS_FILE.is_file():
        print(f"Error: Client secrets file not found at: {CLIENT_SECRETS_FILE}")
        return

    try:
        from google_auth_oauthlib.flow import Flow
    except ImportError:
        print("Please install google-auth-oauthlib: pip install google-auth-oauthlib")
        return

    redirect_uri = "http://127.0.0.1:8000/api/gmail/oauth-callback"

    flow = Flow.from_client_secrets_file(
        str(CLIENT_SECRETS_FILE),
        scopes=SCOPES,
        redirect_uri=redirect_uri,
    )

    auth_url, _ = flow.authorization_url(
        access_type="offline",
        include_granted_scopes="true",
        prompt="consent",
    )

    print("\n1. Opening browser for authorization...", flush=True)
    print("If browser doesn't open automatically, visit this URL:", flush=True)
    print(auth_url, flush=True)
    print("\nWaiting for authorization callback on port 8000...", flush=True)

    webbrowser.open(auth_url)

    HTTPServer.allow_reuse_address = True
    server = HTTPServer(("127.0.0.1", 8000), OAuthCallbackHandler)
    while OAuthCallbackHandler.auth_code is None:
        server.handle_request()

    code = OAuthCallbackHandler.auth_code
    print("\nAuthorization code received! Exchanging for token...", flush=True)

    flow.fetch_token(code=code)
    creds = flow.credentials

    # Save to gmail_token.json
    TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    TOKEN_FILE.write_text(creds.to_json(), encoding="utf-8")

    print("\n" + "=" * 60, flush=True)
    print("SUCCESS! Token with 'gmail.send' saved to secrets/gmail_token.json", flush=True)
    print("=" * 60, flush=True)

    client_id = creds.client_id
    client_secret = creds.client_secret
    refresh_token = creds.refresh_token

    # Auto update local .env and backend/.env if present
    env_content = (
        f"\n# ---- Gmail Official REST API (HTTPS:443) ----\n"
        f"GMAIL_CLIENT_ID={client_id}\n"
        f"GMAIL_CLIENT_SECRET={client_secret}\n"
        f"GMAIL_REFRESH_TOKEN={refresh_token}\n"
        f"GMAIL_SENDER=averis.demo@gmail.com\n"
    )
    for env_path in (BACKEND_DIR.parent / ".env", BACKEND_DIR / ".env"):
        if env_path.is_file():
            text = env_path.read_text(encoding="utf-8")
            if "GMAIL_REFRESH_TOKEN=" not in text:
                env_path.write_text(text.rstrip() + "\n" + env_content, encoding="utf-8")

    print("\n[Render Environment Configuration]", flush=True)
    print("Add these variables to Render (.env or Environment Variables):", flush=True)
    print("-" * 60, flush=True)
    print(f"GMAIL_CLIENT_ID={client_id}", flush=True)
    print(f"GMAIL_CLIENT_SECRET={client_secret}", flush=True)
    print(f"GMAIL_REFRESH_TOKEN={refresh_token}", flush=True)
    print("GMAIL_SENDER=averis.demo@gmail.com", flush=True)
    print("-" * 60, flush=True)


if __name__ == "__main__":
    main()
