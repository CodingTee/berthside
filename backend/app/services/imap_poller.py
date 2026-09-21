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
import re
import urllib.parse
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


def _extract_part_bytes(part: email.message.Message) -> Optional[bytes]:
    payload = part.get_payload(decode=True)
    if isinstance(payload, bytes):
        return payload
    inner = part.get_payload()
    if isinstance(inner, list) and inner and isinstance(inner[0], email.message.Message):
        return inner[0].as_bytes()
    if isinstance(inner, email.message.Message):
        return inner.as_bytes()
    return None


def _collect_nested_attachments(name: str, payload: bytes, ingest_dir: Path, depth: int = 0) -> List[Dict[str, Any]]:
    if depth >= 3:
        return []
    results: List[Dict[str, Any]] = []

    from app.services import archive, nested_mail

    # 1. Expand nested .eml / .msg messages (e.g. Gmail "Forward as attachment")
    if nested_mail.is_nested_mail(name, payload):
        expansion = nested_mail.expand_mail(name, payload)
        for member_name, member_bytes in expansion.members:
            results.extend(_collect_nested_attachments(member_name, member_bytes, ingest_dir, depth + 1))
        return results

    # 2. Expand nested archives (.zip / .tar / etc.)
    if archive.detect_container(name, payload) is not None:
        expansion = archive.expand_archive(name, payload)
        for member_name, member_bytes in expansion.members:
            results.extend(_collect_nested_attachments(member_name, member_bytes, ingest_dir, depth + 1))
        return results

    # 3. Save standard document (PDF, txt, xlsx, etc.)
    safe_name = os.path.basename(name).replace(" ", "_")
    save_path = ingest_dir / safe_name
    try:
        save_path.write_bytes(payload)
    except Exception as exc:
        log.warning("Could not write attachment %s to %s: %s", safe_name, save_path, exc)
    results.append({
        "filename": safe_name,
        "filepath": str(save_path),
        "bytes": payload,
    })
    return results


def extract_email_payload(msg: email.message.Message, ingest_dir: Path) -> Tuple[str, List[Dict[str, Any]]]:
    """Extract plain text body and save attachments to disk, unpacking .eml / .msg containers."""
    body_parts: List[str] = []
    html_parts: List[str] = []
    attachments: List[Dict[str, Any]] = []

    def _traverse(part: email.message.Message) -> None:
        content_type = part.get_content_type()
        disposition = str(part.get("Content-Disposition") or "")

        filename = part.get_filename()
        if filename:
            filename = decode_mime_str(filename)

        is_attachment = "attachment" in disposition.lower() or bool(filename) or (content_type == "message/rfc822")
        if is_attachment:
            raw_bytes = _extract_part_bytes(part)
            if raw_bytes:
                att_name = filename or f"forwarded_message_{len(attachments)+1}.eml"
                expanded = _collect_nested_attachments(att_name, raw_bytes, ingest_dir, 0)
                attachments.extend(expanded)
            return

        if part.is_multipart():
            payload = part.get_payload()
            if isinstance(payload, list):
                for child in payload:
                    if isinstance(child, email.message.Message):
                        _traverse(child)
            return

        if content_type == "text/plain":
            payload = part.get_payload(decode=True)
            if payload:
                body_parts.append(payload.decode(part.get_content_charset() or "utf-8", errors="replace"))
        elif content_type == "text/html":
            payload = part.get_payload(decode=True)
            if payload:
                html_parts.append(payload.decode(part.get_content_charset() or "utf-8", errors="replace"))

    _traverse(msg)

    body_text = "\n\n".join(b.strip() for b in body_parts if b.strip())
    if not body_text and html_parts:
        from app.services.nested_mail import _html_to_text
        body_text = "\n\n".join(_html_to_text(h) for h in html_parts if h.strip())

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

            # Deterministic Classification
            from app.services import classifier
            cls_info = classifier.classify({
                "subject": subject,
                "body": body,
                "from": sender_email or raw_sender,
                "attachments": att_names,
            })
            category = cls_info.category
            confidence = cls_info.confidence

            # Security and Spam Status Gate
            if security_status == "BLOCKED":
                stage_status = "QUARANTINED"
                ai_reason = f"Quarantined: dangerous attachment detected ({', '.join(d['filename'] for d in security_details if not d['is_safe'])})"
            elif category == "SPAM":
                stage_status = "QUARANTINED"
                ai_reason = f"Quarantined: {cls_info.reason}"
            else:
                stage_status = "APPROVED"
                ai_reason = f"Classified as {category} ({cls_info.reason})"

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
                category=category,
                confidence=confidence,
                ai_reason=ai_reason,
                status=stage_status,
                ai_engine="deterministic-rules (local regex)",
            )
            db.add(staged)
            db.commit()
            db.refresh(staged)

            # Ingest into workflow pipeline ONLY if clean AND category is BL_COMPARISON
            report_summary = None
            rep = None
            if security_status == "CLEAN" and category == "BL_COMPARISON":
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

            # Strict Auto-Reply Gatekeeper:
            # Prevents backscatter spam, ignores non-shipping noise, and silences quarantined payloads.
            smtp_result = None
            should_reply = False
            suppression_reason = ""

            if not settings.auto_reply_on_verification:
                suppression_reason = "Auto-reply disabled in settings"
            elif not sender_email:
                suppression_reason = "No sender email address"
            elif security_status != "CLEAN" or stage_status == "QUARANTINED":
                # SILENT DROP / QUARANTINE: Never respond to malware or quarantined files
                suppression_reason = f"Security suppression: {security_status} / {stage_status} (Silent drop, zero backscatter)"
            elif category == "SPAM":
                # SILENT DROP: Never respond to spam
                suppression_reason = f"Spam suppression: detected as spam/phishing ({cls_info.reason})"
            elif category != "BL_COMPARISON":
                suppression_reason = f"Category suppression: non-shipping comparison category ({category})"
            elif not att_names:
                suppression_reason = "Content suppression: no document attachments present"
            elif not rep or not rep.status:
                suppression_reason = "Pipeline suppression: no comparison report generated"
            elif rep.status not in ("OK", "MISMATCH", "NEEDS_REVIEW"):
                suppression_reason = f"Status suppression: report status ({rep.status}) is an unhandled internal error"
            else:
                should_reply = True

            if not should_reply:
                log.info("Auto-reply suppressed for stage %s: %s", stage_id, suppression_reason)
            else:
                from app.services.email_utils import clean_subject, extract_original_sender
                import urllib.parse

                clean_subj = clean_subject(subject)
                orig_client = extract_original_sender(body)
                orig_client_line = f"• Original Client: {orig_client}\n" if (orig_client and orig_client != sender_email) else ""

                target_client = orig_client or sender_email
                is_mismatch = rep.status == "MISMATCH"
                is_review = rep.status == "NEEDS_REVIEW"

                if is_mismatch:
                    reply_subj = f"Re: {clean_subj} - B/L Discrepancies Flagged - Ref #{stage_id}"
                    status_desc = f"Discrepancies identified ({report_summary})"
                elif is_review:
                    review_msg = rep.review_reason or "Pending Review"
                    reply_subj = f"Re: {clean_subj} - Document Review Notice ({review_msg}) - Ref #{stage_id}"
                    status_desc = f"Held for operational review ({review_msg})"
                else:
                    reply_subj = f"Re: {clean_subj} - Document Verification Complete - Ref #{stage_id}"
                    status_desc = f"Verified with 0 discrepancies ({report_summary})"

                # Construct 1-click reply for operator
                client_subj = f"Re: {clean_subj} - {'B/L Discrepancies Flagged' if is_mismatch else 'Document Verification Complete'}"
                client_reply_text = (
                    f"Dear Customer,\n\n"
                    f"Regarding your shipping document submission for '{clean_subj}':\n\n"
                    f"Our automated verification gateway has audited the package:\n"
                    f"• Status: {status_desc}\n"
                    f"• Reference ID: {stage_id}\n\n"
                    f"Best regards,\n"
                    f"Shipping Documentation Operations Desk"
                )
                mailto_link = f"mailto:{target_client}?{urllib.parse.urlencode({'subject': client_subj, 'body': client_reply_text})}"
                # Construct pinpoint Gmail operator search: from:source_email + subject:(keywords)
                safe_subj_kw = re.sub(r'[^\w\s-]', ' ', clean_subj).strip()
                if safe_subj_kw:
                    thread_query = f"from:{target_client} subject:({safe_subj_kw})"
                else:
                    thread_query = f"from:{target_client}"
                gmail_thread_search = f"https://mail.google.com/mail/u/0/#search/{urllib.parse.quote_plus(thread_query)}"

                action_block_text = ""
                if orig_client and orig_client != sender_email:
                    action_block_text = (
                        f"\n----------------------------------------------------------------------\n"
                        f"🚀 [Fast Client Reply Gateway (Zero Manual Forwarding · Direct Re:)]\n"
                        f"Original client detected: {orig_client}\n\n"
                        f"✉️ [Option 1: One-Click Reply (Auto-filled mailto)]:\n"
                        f"👉 {mailto_link}\n\n"
                        f"🔍 [Option 2: Locate Exact Thread in Gmail (from:{orig_client} + Subject)]:\n"
                        f"👉 {gmail_thread_search}\n\n"
                        f"📋 [Pre-formatted Client Reply (Copy & paste into thread)]:\n"
                        f"----------------------------------------------------------------------\n"
                        f"{client_reply_text}\n"
                        f"----------------------------------------------------------------------\n"
                        f"----------------------------------------------------------------------\n"
                    )

                reply_body = (
                    f"Dear Shipping Documentation Team / Customer,\n\n"
                    f"Regarding your submission for '{clean_subj}':\n\n"
                    f"Our automated verification gateway has audited the package:\n"
                    f"• Status: {status_desc}\n"
                    f"• Reference ID: {stage_id}\n"
                    f"• Inbound Channel: IMAP Live Gateway\n"
                    f"{orig_client_line}\n"
                    f"{action_block_text}\n"
                    f"Best regards,\n"
                    f"Documentation Operations Desk\n"
                    f"{staged.source_mailbox}"
                )

                # Rich HTML representation with 1-click action button and copyable draft box
                html_btn_html = ""
                if orig_client and orig_client != sender_email:
                    html_btn_html = f"""
                    <div style="margin: 20px 0; padding: 16px; background-color: #f0fdf4; border: 1px solid #bbf7d0; border-radius: 8px;">
                        <h4 style="margin: 0 0 8px 0; color: #166534; font-size: 15px;">🚀 Fast Client Reply Gateway (Direct Re: · No Fwd: Noise)</h4>
                        <p style="margin: 0 0 14px 0; color: #374151; font-size: 13px; line-height: 1.5;">Original client identified: <strong>{orig_client}</strong>. Select an action below:</p>
                        <div style="display: flex; flex-wrap: wrap; gap: 10px; margin-bottom: 14px;">
                            <a href="{mailto_link}" style="display: inline-block; background-color: #2563eb; color: #ffffff; padding: 10px 18px; text-decoration: none; border-radius: 6px; font-weight: bold; font-size: 13px; margin: 4px 8px 6px 0; text-align: center;">✉️ One-Click Reply to Client</a>
                            <a href="{gmail_thread_search}" style="display: inline-block; background-color: #059669; color: #ffffff; padding: 10px 18px; text-decoration: none; border-radius: 6px; font-weight: bold; font-size: 13px; margin: 4px 0 6px 0; text-align: center;">🔍 Locate Exact Thread in Gmail (from:{orig_client})</a>
                        </div>
                        <div style="margin-top: 12px; padding-top: 12px; border-top: 1px dashed #cbd5e1;">
                            <p style="margin: 0 0 6px 0; font-size: 12px; font-weight: 600; color: #475569;">📋 Pre-formatted Client Reply (Copy & paste into thread):</p>
                            <div style="background-color: #ffffff; border: 1px solid #cbd5e1; border-radius: 6px; padding: 12px; font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace; font-size: 12px; color: #1e293b; white-space: pre-wrap; line-height: 1.5; user-select: all;">{client_reply_text}</div>
                        </div>
                    </div>
                    """

                html_body = f"""
                <div style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; line-height: 1.6; color: #1f2937;">
                    <h3 style="color: #111827; border-bottom: 2px solid #e5e7eb; padding-bottom: 8px;">Averis Automated Shipping Documentation Receipt</h3>
                    <p>Dear Shipping Documentation Team / Customer,</p>
                    <p>Regarding your submission for <strong>{clean_subj}</strong>:</p>
                    <div style="background-color: #f3f4f6; padding: 12px 16px; border-radius: 6px; margin: 16px 0;">
                        <ul style="margin: 0; padding-left: 20px;">
                            <li><strong>Status:</strong> {report_summary}</li>
                            <li><strong>Reference ID:</strong> {stage_id}</li>
                            <li><strong>Inbound Channel:</strong> IMAP Live Gateway</li>
                            {f"<li><strong>Detected Original Client:</strong> {orig_client}</li>" if orig_client else ""}
                        </ul>
                    </div>
                    {html_btn_html}
                    <p style="color: #6b7280; font-size: 13px; margin-top: 24px;">Best regards,<br><strong>Documentation Operations Desk</strong><br>{staged.source_mailbox}</p>
                </div>
                """

                smtp_result = dispatch_smtp_email(
                    to_email=sender_email,
                    subject=reply_subj,
                    body=reply_body,
                    html_body=html_body,
                    in_reply_to=message_id,
                    references=references,
                    sender=settings.smtp_from or settings.imap_user,
                )

                # Persist dispatch record
                rec = DispatchRecord(
                    stage_id=stage_id,
                    source_mailbox=staged.source_mailbox,
                    recipient=sender_email,
                    decision="MISMATCH" if is_mismatch else "VERIFIED",
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
