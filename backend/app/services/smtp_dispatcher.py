"""Outbound SMTP Dispatcher for Enterprise SDOC Hub.

Handles real SMTP dispatch to external clients (e.g. user's private Gmail)
with RFC-compliant thread tracking headers (In-Reply-To, References, Message-ID).

Gracefully falls back to SIMULATED delivery when SMTP credentials are not configured
or during automated test execution.
"""
from __future__ import annotations

import email.utils
import logging
import smtplib
import time
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Any, Dict, List, Optional

from app.config import get_settings

log = logging.getLogger(__name__)


def dispatch_smtp_email(
    to_email: str,
    subject: str,
    body: str,
    html_body: Optional[str] = None,
    in_reply_to: Optional[str] = None,
    references: Optional[str] = None,
    sender: Optional[str] = None,
) -> Dict[str, Any]:
    """Send an outbound email via live SMTP, or fallback to SIMULATED delivery."""
    settings = get_settings()

    # 1. Fallback check: if no host configured, record as SIMULATED
    if not settings.smtp_host or not settings.smtp_host.strip():
        log.info("SMTP host not configured. Fallback to SIMULATED delivery for %s", to_email)
        return {
            "delivery": "SIMULATED",
            "status": "FALLBACK_SIMULATED",
            "message_id": f"<sim-{int(time.time()*1000)}@sdoc.local>",
            "to": to_email,
            "subject": subject,
            "channel": "SIMULATED",
        }

    from_addr = sender or settings.smtp_from or settings.smtp_user
    if not from_addr:
        from_addr = "sdoc-hub@averis.com"

    domain = from_addr.split("@")[-1] if "@" in from_addr else "sdoc.hub"
    msg_id = email.utils.make_msgid(domain=domain)

    # 2. Build MIME message
    if html_body:
        msg = MIMEMultipart("alternative")
        msg.attach(MIMEText(body, "plain", "utf-8"))
        msg.attach(MIMEText(html_body, "html", "utf-8"))
    else:
        msg = MIMEText(body, "plain", "utf-8")

    msg["From"] = from_addr
    msg["To"] = to_email
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

    # 3. Connect and send
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
        log.info("Live SMTP email sent successfully to %s, Subject: %s", to_email, subject)
        return {
            "delivery": "SENT_SMTP",
            "status": "SUCCESS",
            "message_id": msg_id,
            "to": to_email,
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
            "subject": subject,
            "channel": f"SMTP_FAILED ({settings.smtp_host})",
        }
