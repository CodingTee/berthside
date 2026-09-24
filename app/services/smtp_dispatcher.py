"""Outbound SMTP & Resend API Dispatcher for Enterprise BerthSide Hub.

Handles real email dispatch to external clients (e.g. user's private Gmail)
with RFC-compliant thread tracking headers (In-Reply-To, References, Message-ID).

Dual-mode architecture:
1. Resend REST API (HTTPS:443): Highly recommended for Render.com and serverless
   deployments where outbound SMTP ports (25/465/587) are blocked.
2. Direct Live SMTP: Standard smtplib transport for standalone servers or local testing.
3. Graceful Fallback: Seamlessly records SIMULATED delivery when credentials are
   unconfigured or during unit testing.
"""
from __future__ import annotations

import base64
import email.utils
import json
import logging
import smtplib
import time
import urllib.parse
import urllib.request
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Any, Dict, List, Optional, Union
from urllib.error import HTTPError

from app.config import get_settings

log = logging.getLogger(__name__)


def _dispatch_via_resend(
    api_key: str,
    from_addr: str,
    to_email: str,
    subject: str,
    body: str,
    html_body: Optional[str] = None,
    in_reply_to: Optional[str] = None,
    references: Optional[str] = None,
    cc: Optional[Union[str, List[str]]] = None,
) -> Dict[str, Any]:
    """Dispatch an email via Resend REST API (POST https://api.resend.com/emails)."""
    url = "https://api.resend.com/emails"

    # Format recipients into list
    if isinstance(to_email, list):
        to_list = [t.strip() for t in to_email if t and t.strip()]
    elif isinstance(to_email, str):
        to_list = [t.strip() for t in to_email.split(",") if t.strip()]
    else:
        to_list = [str(to_email).strip()]

    cc_list: List[str] = []
    if cc:
        if isinstance(cc, list):
            cc_list = [c.strip() for c in cc if c and c.strip()]
        elif isinstance(cc, str):
            cc_list = [c.strip() for c in cc.split(",") if c.strip()]

    payload: Dict[str, Any] = {
        "from": from_addr,
        "to": to_list,
        "subject": subject,
        "text": body,
    }
    if html_body:
        payload["html"] = html_body
    if cc_list:
        payload["cc"] = cc_list

    headers_dict: Dict[str, str] = {}
    if in_reply_to:
        clean_irt = in_reply_to.strip()
        if not clean_irt.startswith("<"):
            clean_irt = f"<{clean_irt}>"
        headers_dict["In-Reply-To"] = clean_irt

    if references:
        clean_refs = references.strip()
        if not clean_refs.startswith("<"):
            clean_refs = f"<{clean_refs}>"
        headers_dict["References"] = clean_refs
    elif in_reply_to:
        headers_dict["References"] = headers_dict.get("In-Reply-To", "")

    if headers_dict:
        payload["headers"] = headers_dict

    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={
            "Authorization": f"Bearer {api_key.strip()}",
            "Content-Type": "application/json",
            "User-Agent": "Federated-Shipping-Hub/1.0",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            resp_body = resp.read().decode("utf-8")
            resp_data = json.loads(resp_body) if resp_body else {}
            msg_id = resp_data.get("id") or f"<resend-{int(time.time()*1000)}@sdoc.hub>"
            log.info("Email dispatched successfully via Resend API to %s, ID: %s", to_email, msg_id)
            return {
                "delivery": "SENT_SMTP",
                "status": "SUCCESS",
                "message_id": msg_id,
                "to": to_email,
                "cc": ", ".join(cc_list) if cc_list else None,
                "subject": subject,
                "channel": "Resend API (HTTPS)",
            }
    except HTTPError as err:
        err_msg = ""
        try:
            err_msg = err.read().decode("utf-8", errors="replace")
        except Exception:
            pass
        log.error("Resend API HTTP error %d: %s", err.code, err_msg or err.reason)
        raise RuntimeError(f"Resend API error ({err.code}): {err_msg or err.reason}")
    except Exception as exc:
        log.error("Resend API transport error: %s", exc)
        raise


_GMAIL_TOKEN_CACHE: Dict[str, Any] = {"access_token": "", "expires_at": 0.0}


def _get_gmail_access_token(client_id: str, client_secret: str, refresh_token: str) -> str:
    """Exchange refresh_token for a fresh access_token via Google OAuth2 token endpoint."""
    now = time.time()
    if _GMAIL_TOKEN_CACHE.get("access_token") and _GMAIL_TOKEN_CACHE.get("expires_at", 0) > now + 60:
        return _GMAIL_TOKEN_CACHE["access_token"]

    token_url = "https://oauth2.googleapis.com/token"
    payload = urllib.parse.urlencode({
        "client_id": client_id.strip(),
        "client_secret": client_secret.strip(),
        "refresh_token": refresh_token.strip(),
        "grant_type": "refresh_token",
    }).encode("utf-8")

    req = urllib.request.Request(
        token_url,
        data=payload,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = json.loads(resp.read().decode("utf-8"))
        access_token = data["access_token"]
        expires_in = data.get("expires_in", 3600)
        _GMAIL_TOKEN_CACHE["access_token"] = access_token
        _GMAIL_TOKEN_CACHE["expires_at"] = now + expires_in
        return access_token


def _dispatch_via_gmail_api(
    client_id: str,
    client_secret: str,
    refresh_token: str,
    from_addr: str,
    to_email: str,
    subject: str,
    body: str,
    html_body: Optional[str] = None,
    in_reply_to: Optional[str] = None,
    references: Optional[str] = None,
    cc: Optional[Union[str, List[str]]] = None,
    thread_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Send an outbound email via Gmail REST API (POST https://gmail.googleapis.com/gmail/v1/users/me/messages/send)."""
    access_token = _get_gmail_access_token(client_id, client_secret, refresh_token)

    cc_str = None
    if cc:
        if isinstance(cc, list):
            cc_str = ", ".join(c.strip() for c in cc if c and c.strip())
        elif isinstance(cc, str) and cc.strip():
            cc_str = cc.strip()

    domain = from_addr.split("@")[-1] if "@" in from_addr else "gmail.com"
    msg_id = email.utils.make_msgid(domain=domain)

    if html_body:
        msg = MIMEMultipart("alternative")
        msg.attach(MIMEText(body, "plain", "utf-8"))
        msg.attach(MIMEText(html_body, "html", "utf-8"))
    else:
        msg = MIMEText(body, "plain", "utf-8")

    msg["From"] = from_addr
    msg["To"] = to_email
    if cc_str:
        msg["Cc"] = cc_str
    msg["Subject"] = subject
    msg["Date"] = email.utils.formatdate(localtime=True)
    msg["Message-ID"] = msg_id

    if in_reply_to:
        clean_irt = in_reply_to.strip()
        if not clean_irt.startswith("<"):
            clean_irt = f"<{clean_irt}>"
        msg["In-Reply-To"] = clean_irt

    if references:
        clean_refs = references.strip()
        if not clean_refs.startswith("<"):
            clean_refs = f"<{clean_refs}>"
        msg["References"] = clean_refs
    elif in_reply_to:
        msg["References"] = msg.get("In-Reply-To", "")

    raw_bytes = msg.as_bytes()
    raw_b64 = base64.urlsafe_b64encode(raw_bytes).decode("ascii")

    send_url = "https://gmail.googleapis.com/gmail/v1/users/me/messages/send"
    post_payload: Dict[str, Any] = {"raw": raw_b64}
    if thread_id:
        post_payload["threadId"] = thread_id

    data = json.dumps(post_payload).encode("utf-8")
    req = urllib.request.Request(
        send_url,
        data=data,
        headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
            "User-Agent": "Federated-Shipping-Hub/1.0",
        },
        method="POST",
    )

    with urllib.request.urlopen(req, timeout=20) as resp:
        resp_data = json.loads(resp.read().decode("utf-8"))
        gmail_id = resp_data.get("id") or msg_id
        gmail_thread = resp_data.get("threadId")
        log.info("Email dispatched successfully via Gmail REST API to %s, Gmail ID: %s", to_email, gmail_id)
        return {
            "delivery": "SENT_SMTP",
            "status": "SUCCESS",
            "message_id": msg_id,
            "gmail_id": gmail_id,
            "thread_id": gmail_thread,
            "to": to_email,
            "cc": cc_str,
            "subject": subject,
            "channel": "Gmail REST API (HTTPS:443)",
        }


def _resolve_gmail_credentials() -> Optional[tuple[str, str, str, str]]:
    """Resolve (client_id, client_secret, refresh_token, sender) if configured."""
    from pathlib import Path
    settings = get_settings()

    client_id = (settings.gmail_client_id or "").strip()
    client_secret = (settings.gmail_client_secret or "").strip()
    refresh_token = (settings.gmail_refresh_token or "").strip()
    from_addr = (settings.gmail_demo_account or "").strip() or "averis.demo@gmail.com"

    # Also resolve client_id / secret from credentials file if refresh_token is given
    if refresh_token and (not client_id or not client_secret):
        for fpath in (settings.gmail_token_file, settings.gmail_credentials_file):
            p = Path(fpath)
            if p.is_file():
                try:
                    cdata = json.loads(p.read_text(encoding="utf-8"))
                    web_inst = cdata.get("web") or cdata.get("installed") or cdata
                    if not client_id and web_inst.get("client_id"):
                        client_id = str(web_inst["client_id"]).strip()
                    if not client_secret and web_inst.get("client_secret"):
                        client_secret = str(web_inst["client_secret"]).strip()
                except Exception:
                    pass

    if client_id and client_secret and refresh_token:
        return client_id, client_secret, refresh_token, from_addr
    return None


def dispatch_smtp_email(
    to_email: str,
    subject: str,
    body: str,
    html_body: Optional[str] = None,
    in_reply_to: Optional[str] = None,
    references: Optional[str] = None,
    sender: Optional[str] = None,
    cc: Optional[Union[str, List[str]]] = None,
) -> Dict[str, Any]:
    """Send an outbound email via Gmail REST API, Resend API, or live SMTP, with fallback to SIMULATED."""
    settings = get_settings()

    # Format CC addresses if provided
    cc_str = None
    if cc:
        if isinstance(cc, list):
            cc_str = ", ".join(c.strip() for c in cc if c and c.strip())
        elif isinstance(cc, str) and cc.strip():
            cc_str = cc.strip()

    # 1. Primary Priority: Gmail Official REST API (HTTPS:443 - zero domain, 100% deliverability, bypasses Render SMTP block)
    gmail_creds = _resolve_gmail_credentials()
    if gmail_creds:
        c_id, c_secret, r_token, default_sender = gmail_creds
        use_sender = sender if (sender and "@" in sender) else default_sender
        log.info("Dispatching email to %s via Gmail REST API (HTTPS:443, From: %s)...", to_email, use_sender)
        try:
            return _dispatch_via_gmail_api(
                client_id=c_id,
                client_secret=c_secret,
                refresh_token=r_token,
                from_addr=use_sender,
                to_email=to_email,
                subject=subject,
                body=body,
                html_body=html_body,
                in_reply_to=in_reply_to,
                references=references,
                cc=cc,
            )
        except Exception as exc:
            log.error("Gmail REST API dispatch failed for %s: %s", to_email, exc)
            if not (settings.resend_api_key or settings.smtp_host):
                return {
                    "delivery": "SIMULATED",
                    "status": "ERROR",
                    "error": str(exc),
                    "message_id": f"<err-gmail-{int(time.time()*1000)}@sdoc.local>",
                    "to": to_email,
                    "cc": cc_str,
                    "subject": subject,
                    "channel": "Gmail REST API (Failed)",
                }
            log.info("Falling back from Gmail REST API to Resend / SMTP...")

    # 2. Secondary: Resend REST API (HTTPS:443 - alternative for Render.com port limitations)
    if settings.resend_api_key and settings.resend_api_key.strip():
        # Resolve from address
        from_addr = settings.resend_from or "Federated Shipping Hub <onboarding@resend.dev>"
        if sender and ("@" in sender) and not settings.resend_from:
            from_addr = sender

        log.info("Dispatching email to %s via Resend REST API (From: %s)...", to_email, from_addr)
        try:
            return _dispatch_via_resend(
                api_key=settings.resend_api_key,
                from_addr=from_addr,
                to_email=to_email,
                subject=subject,
                body=body,
                html_body=html_body,
                in_reply_to=in_reply_to,
                references=references,
                cc=cc,
            )
        except Exception as exc:
            log.error("Resend API dispatch failed for %s: %s", to_email, exc)
            # If SMTP host is configured as backup, fall through to SMTP
            if not (settings.smtp_host and settings.smtp_host.strip()):
                return {
                    "delivery": "SIMULATED",
                    "status": "ERROR",
                    "error": str(exc),
                    "message_id": f"<err-resend-{int(time.time()*1000)}@sdoc.local>",
                    "to": to_email,
                    "cc": cc_str,
                    "subject": subject,
                    "channel": "Resend API (Failed)",
                }
            log.info("Falling back from Resend to configured SMTP host %s...", settings.smtp_host)

    # 2. Secondary: Direct Live SMTP (if SMTP host configured)
    if not settings.smtp_host or not settings.smtp_host.strip():
        log.info("Neither Resend API key nor SMTP host configured. Fallback to SIMULATED delivery for %s", to_email)
        return {
            "delivery": "SIMULATED",
            "status": "FALLBACK_SIMULATED",
            "message_id": f"<sim-{int(time.time()*1000)}@sdoc.local>",
            "to": to_email,
            "cc": cc_str,
            "subject": subject,
            "channel": "SIMULATED",
        }

    from_addr = sender or settings.smtp_from or settings.smtp_user
    if not from_addr:
        from_addr = "sdoc-hub@averis.com"

    domain = from_addr.split("@")[-1] if "@" in from_addr else "sdoc.hub"
    msg_id = email.utils.make_msgid(domain=domain)

    # Build MIME message
    if html_body:
        msg = MIMEMultipart("alternative")
        msg.attach(MIMEText(body, "plain", "utf-8"))
        msg.attach(MIMEText(html_body, "html", "utf-8"))
    else:
        msg = MIMEText(body, "plain", "utf-8")

    msg["From"] = from_addr
    msg["To"] = to_email
    if cc_str:
        msg["Cc"] = cc_str
    msg["Subject"] = subject
    msg["Date"] = email.utils.formatdate(localtime=True)
    msg["Message-ID"] = msg_id

    # RFC 2822 / 5322 Thread Preservation Headers
    if in_reply_to:
        clean_irt = in_reply_to.strip()
        if not clean_irt.startswith("<"):
            clean_irt = f"<{clean_irt}>"
        msg["In-Reply-To"] = clean_irt

    if references:
        clean_refs = references.strip()
        if not clean_refs.startswith("<"):
            clean_refs = f"<{clean_refs}>"
        msg["References"] = clean_refs
    elif in_reply_to:
        msg["References"] = msg.get("In-Reply-To")

    # Connect and send via smtplib
    try:
        if settings.smtp_use_ssl:
            server = smtplib.SMTP_SSL(settings.smtp_host, settings.smtp_port, timeout=15)
        else:
            server = smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=15)
            if settings.smtp_use_tls:
                server.ehlo()
                server.starttls()
                server.ehlo()

        if settings.smtp_user and settings.smtp_password:
            pwd = settings.smtp_password.strip()
            if "gmail.com" in (settings.smtp_host or "").lower() and " " in pwd:
                pwd = pwd.replace(" ", "")
            server.login(settings.smtp_user.strip(), pwd)

        server.send_message(msg)
        server.quit()
        log.info("Live SMTP email sent successfully to %s (CC: %s), Subject: %s", to_email, cc_str, subject)
        return {
            "delivery": "SENT_SMTP",
            "status": "SUCCESS",
            "message_id": msg_id,
            "to": to_email,
            "cc": cc_str,
            "subject": subject,
            "channel": f"SMTP ({settings.smtp_host})",
        }
    except Exception as exc:
        log.error("Failed to dispatch email via SMTP to %s: %s", to_email, exc)
        return {
            "delivery": "SIMULATED",
            "status": "ERROR",
            "error": str(exc),
            "message_id": msg_id,
            "to": to_email,
            "cc": cc_str,
            "subject": subject,
            "channel": f"SMTP_FAILED ({settings.smtp_host})",
        }
