"""Inbound IMAP Poller for Enterprise SDOC Hub.

Polls a designated intake mailbox (e.g. Gmail/Outlook/Enterprise mailbox via IMAP4_SSL),
extracts customer transmissions with SI and B/L attachments, passes them through the
security scanner and local deterministic rule engine, and optionally triggers an automated
SMTP reply back to the sender.
"""
from __future__ import annotations

import datetime as dt
import email
import email.utils
import imaplib
import logging
import os
from email.header import decode_header
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import (
    DispatchRecord,
    EmailRecord,
    ReportRecord,
    StagedEmailRecord,
    utcnow,
)
from app.services.security import verify_file_safety
from app.services.smtp_dispatcher import dispatch_smtp_email
from app.services import workflow

log = logging.getLogger(__name__)


def decode_mime_str(header_val: Optional[str]) -> str:
    """Safely decode RFC2047 MIME encoded-word headers."""
    if not header_val:
        return ""
    parts = []
    try:
        for text, enc in decode_header(header_val):
            if isinstance(text, bytes):
                parts.append(text.decode(enc or "utf-8", errors="replace"))
            else:
                parts.append(str(text))
        return "".join(parts)
    except Exception:
        return str(header_val)


def extract_email_payload(msg: email.message.Message, ingest_dir: Path) -> Tuple[str, List[Dict[str, Any]]]:
    """Extract plain text body and save attachments to disk."""
    body_text = ""
    attachments: List[Dict[str, Any]] = []

    for part in msg.walk():
        content_type = part.get_content_type()
        disposition = str(part.get("Content-Disposition") or "")

        filename = part.get_filename()
        if filename:
            filename = decode_mime_str(filename)

        if "attachment" in disposition.lower() or filename:
            payload = part.get_payload(decode=True)
            if payload and filename:
                safe_name = os.path.basename(filename).replace(" ", "_")
                save_path = ingest_dir / safe_name
                try:
                    save_path.write_bytes(payload)
                except Exception as exc:
                    log.warning("Could not write attachment %s to %s: %s", safe_name, save_path, exc)
                attachments.append({
                    "filename": safe_name,
                    "filepath": str(save_path),
                    "bytes": payload,
                })
        elif content_type == "text/plain" and not body_text:
            payload = part.get_payload(decode=True)
            if payload:
                body_text = payload.decode(part.get_content_charset() or "utf-8", errors="replace")

    return body_text.strip(), attachments


def poll_imap_inbox(db: Session) -> Dict[str, Any]:
    """Connect to IMAP server, fetch UNSEEN emails, process, and optionally auto-reply."""
    settings = get_settings()

    if not settings.imap_host or not settings.imap_host.strip():
        log.info("IMAP host not configured. IMAP polling skipped.")
        return {
            "status": "SKIPPED",
            "message": "IMAP host is not configured in settings (.env).",
            "polled_count": 0,
            "processed": [],
        }

    ingest_dir = Path(settings.ingest_dir)
    ingest_dir.mkdir(parents=True, exist_ok=True)

    processed_stages: List[Dict[str, Any]] = []

    try:
        mail = imaplib.IMAP4_SSL(settings.imap_host, settings.imap_port, timeout=20)
        pwd = settings.imap_password.strip()
        if "gmail.com" in (settings.imap_host or "").lower() and " " in pwd:
            pwd = pwd.replace(" ", "")
        mail.login(settings.imap_user.strip(), pwd)
        mail.select(settings.imap_folder)

        typ, msg_ids = mail.search(None, "UNSEEN")
        if typ != "OK" or not msg_ids or not msg_ids[0]:
            mail.close()
            mail.logout()
            return {
                "status": "SUCCESS",
                "message": "Connected to IMAP. No new unread messages.",
                "polled_count": 0,
                "processed": [],
            }

        id_list = msg_ids[0].split()
        log.info("Found %d unread emails in %s@%s", len(id_list), settings.imap_user, settings.imap_host)

        for num in id_list:
            fetch_res, data = mail.fetch(num, "(RFC822)")
            if fetch_res != "OK" or not data or not data[0]:
                continue

            raw_bytes = data[0][1]
            msg = email.message_from_bytes(raw_bytes)

            subject = decode_mime_str(msg.get("Subject", "(No Subject)"))
            raw_sender = msg.get("From", "")
            sender_name, sender_email = email.utils.parseaddr(raw_sender)
            message_id = msg.get("Message-ID", "")
            references = msg.get("References", "") or message_id

            body, attachments = extract_email_payload(msg, ingest_dir)

            # Security Inspection
            security_status = "CLEAN"
            security_details = []
            att_names = []
            for att in attachments:
                safe, reason = verify_file_safety(att["filename"], att["bytes"])
                security_details.append({"filename": att["filename"], "is_safe": safe, "reason": reason})
                att_names.append(att["filename"])
                if not safe:
                    security_status = "BLOCKED"

            now_str = dt.datetime.now().strftime("%H%M%S%f")[:9]
            stage_id = f"STG-IMAP-{now_str}"

            # Staging row
            staged = StagedEmailRecord(
                stage_id=stage_id,
                source_mailbox=settings.imap_user or "imap.inbound@averis.com",
                sender=sender_email or raw_sender,
                recipient=settings.imap_user or "sdoc-hub@averis.com",
                subject=subject,
                body=body,
                attachments=att_names,
                security_status=security_status,
                security_details=security_details,
                category="BL_COMPARISON" if att_names else "INVOICE_QUERY",
                confidence=1.0,
                ai_reason="Live IMAP external ingestion via deterministic rule pipeline",
                status="QUARANTINED" if security_status == "BLOCKED" else "APPROVED",
                ai_engine="deterministic-rules (local regex)",
            )
            db.add(staged)
            db.commit()
            db.refresh(staged)

            # Ingest into workflow pipeline if clean
            report_summary = "Security check passed"
            if security_status == "CLEAN":
                email_id = f"INGEST-{stage_id}"
                email_rec = EmailRecord(
                    email_id=email_id,
                    sender=sender_email or raw_sender,
                    subject=subject,
                    body=body,
                    attachments=att_names,
                    received_at=staged.received_at,
                    source_mailbox=staged.source_mailbox,
                )
                db.add(email_rec)
                db.commit()
                try:
                    workflow.process_email(db, email_id)
                    rep = db.query(ReportRecord).filter_by(email_id=email_id).first()
                    if rep:
                        report_summary = f"Verdict: {rep.status}, Category: {rep.category}"
                except Exception as exc:
                    log.warning("Workflow pipeline error for %s: %s", email_id, exc)

            # Auto-reply via SMTP if enabled
            smtp_result = None
            if settings.auto_reply_on_verification and sender_email:
                is_rejection = staged.status == "REJECTED" or security_status == "BLOCKED"
                if is_rejection:
                    reply_subj = f"Re: {subject} - B/L Document Amendment Required (Rejected) - Ref #{stage_id}"
                    reply_body = (
                        f"Dear Shipping Documentation Team / Customer,\n\n"
                        f"Regarding your submission for '{subject}':\n\n"
                        f"Our automated documentation gateway has audited the package and flagged discrepancies or safety issues:\n"
                        f"• Status: {staged.status}\n"
                        f"• Reference: {stage_id}\n\n"
                        f"Please review and submit a revised version.\n\n"
                        f"Best regards,\n"
                        f"Documentation Operations Desk\n"
                        f"{staged.source_mailbox}"
                    )
                else:
                    reply_subj = f"Re: {subject} - Document Verification Complete - Ref #{stage_id}"
                    reply_body = (
                        f"Dear Shipping Documentation Team / Customer,\n\n"
                        f"Thank you for your submission for '{subject}'.\n\n"
                        f"Our automated verification pipeline has verified your documents:\n"
                        f"• Status: {report_summary}\n"
                        f"• Reference: {stage_id}\n"
                        f"• Inbound Channel: IMAP Live Gateway\n\n"
                        f"Best regards,\n"
                        f"Documentation Operations Desk\n"
                        f"{staged.source_mailbox}"
                    )

                smtp_result = dispatch_smtp_email(
                    to_email=sender_email,
                    subject=reply_subj,
                    body=reply_body,
                    in_reply_to=message_id,
                    references=references,
                    sender=settings.smtp_from or settings.imap_user,
                )

                # Persist dispatch record
                rec = DispatchRecord(
                    stage_id=stage_id,
                    source_mailbox=staged.source_mailbox,
                    recipient=sender_email,
                    decision="REJECTED" if is_rejection else "VERIFIED",
                    subject=reply_subj,
                    body=reply_body,
                    channel=smtp_result.get("channel", "SMTP"),
                    delivery=smtp_result.get("delivery", "SIMULATED"),
                )
                db.add(rec)
                staged.status = "RETURNED"
                db.commit()

            # Mark as read
            mail.store(num, "+FLAGS", "\\Seen")

            processed_stages.append({
                "stage_id": stage_id,
                "sender": sender_email,
                "subject": subject,
                "security_status": security_status,
                "smtp_delivery": smtp_result.get("delivery") if smtp_result else None,
            })

        mail.close()
        mail.logout()

        return {
            "status": "SUCCESS",
            "message": f"Polled and processed {len(processed_stages)} new emails.",
            "polled_count": len(processed_stages),
            "processed": processed_stages,
        }

    except Exception as exc:
        log.error("IMAP polling failed: %s", exc)
        return {
            "status": "ERROR",
            "error": str(exc),
            "polled_count": 0,
            "processed": [],
        }
