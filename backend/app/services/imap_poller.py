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

_PROCESSED_MESSAGE_KEYS: set[str] = set()


def _get_processed_keys(db: Session) -> set[str]:
    """Retrieve or pre-populate the set of already processed message IDs / signatures."""
    global _PROCESSED_MESSAGE_KEYS
    if not _PROCESSED_MESSAGE_KEYS:
        try:
            for row in db.query(StagedEmailRecord).order_by(StagedEmailRecord.id.desc()).limit(200).all():
                if row.subject:
                    _PROCESSED_MESSAGE_KEYS.add(row.subject.strip().lower())
                if isinstance(row.security_details, list):
                    for d in row.security_details:
                        if isinstance(d, dict) and d.get("message_id"):
                            _PROCESSED_MESSAGE_KEYS.add(d["message_id"].strip())
            for row in db.query(DispatchRecord).order_by(DispatchRecord.id.desc()).limit(200).all():
                if row.gmail_message_id:
                    _PROCESSED_MESSAGE_KEYS.add(row.gmail_message_id.strip())
        except Exception as exc:
            log.warning("Could not pre-populate processed message keys: %s", exc)
    return _PROCESSED_MESSAGE_KEYS


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
    data = None
    if isinstance(inner, list) and inner and isinstance(inner[0], email.message.Message):
        data = inner[0].as_bytes()
    elif isinstance(inner, email.message.Message):
        data = inner.as_bytes()
    elif isinstance(inner, str):
        data = inner.encode("utf-8")
    elif isinstance(inner, bytes):
        data = inner

    if data:
        cte = (part.get("Content-Transfer-Encoding") or "").lower().strip()
        if cte == "base64":
            try:
                import base64
                data = base64.b64decode(data.strip())
            except Exception:
                pass
    return data


def find_nested_email_attachments(msg: email.message.Message) -> List[Tuple[str, bytes]]:
    """Identify and collect nested .eml / .msg messages attached to this email."""
    nested_list: List[Tuple[str, bytes]] = []

    def _traverse(part: email.message.Message) -> None:
        content_type = part.get_content_type().lower()
        disposition = str(part.get("Content-Disposition") or "").lower()
        filename = part.get_filename()
        if filename:
            filename = decode_mime_str(filename)

        is_eml = (
            content_type == "message/rfc822"
            or (filename and filename.lower().endswith((".eml", ".msg")))
        )
        if is_eml or ("attachment" in disposition) or filename:
            raw_bytes = _extract_part_bytes(part)
            if raw_bytes:
                from app.services import nested_mail
                att_name = filename or f"forwarded_message_{len(nested_list)+1}.eml"
                if is_eml or nested_mail.is_nested_mail(att_name, raw_bytes):
                    nested_list.append((att_name, raw_bytes))
                    return  # Do not descend inside this nested email container

        if part.is_multipart():
            payload = part.get_payload()
            if isinstance(payload, list):
                for child in payload:
                    if isinstance(child, email.message.Message):
                        _traverse(child)

    _traverse(msg)
    return nested_list


def has_direct_document_attachments(msg: email.message.Message) -> bool:
    """Return True if the outer email itself has non-nested file attachments."""
    if not msg.is_multipart():
        return False
    payload = msg.get_payload()
    if not isinstance(payload, list):
        return False
    for part in payload:
        if isinstance(part, email.message.Message):
            content_type = part.get_content_type().lower()
            disposition = str(part.get("Content-Disposition") or "").lower()
            filename = part.get_filename()
            if filename:
                filename = decode_mime_str(filename).lower()
            is_eml = content_type == "message/rfc822" or (filename and filename.endswith((".eml", ".msg")))
            if not is_eml and ("attachment" in disposition or filename):
                return True
    return False



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



def _stage_and_verify_single_email(
    db: Session,
    stage_id: str,
    subject: str,
    body: str,
    sender_email: str,
    raw_sender: str,
    attachments: List[Dict[str, Any]],
    source_mailbox: str,
    settings: Any,
    batch_info: Optional[Tuple[int, int]] = None,
    message_id: Optional[str] = None,
    references: Optional[str] = None,
) -> Dict[str, Any]:
    """Inspect safety, classify, create StagedEmailRecord, ingest to pipeline, and optionally auto-reply."""
    # 1. Security Inspection
    security_status = "CLEAN"
    security_details = []
    att_names = []
    for att in attachments:
        safe, reason = verify_file_safety(att["filename"], att["bytes"])
        security_details.append({"filename": att["filename"], "is_safe": safe, "reason": reason})
        att_names.append(att["filename"])
        if not safe:
            security_status = "BLOCKED"

    if message_id:
        security_details.append({"type": "envelope_meta", "message_id": message_id.strip()})
        _PROCESSED_MESSAGE_KEYS.add(message_id.strip())
    if subject:
        _PROCESSED_MESSAGE_KEYS.add(subject.strip().lower())

    # 2. Deterministic Classification
    from app.services import classifier
    cls_info = classifier.classify({
        "subject": subject,
        "body": body,
        "from": sender_email or raw_sender,
        "attachments": att_names,
    })
    category = cls_info.category
    confidence = cls_info.confidence

    # 3. Security and Spam Status Gate
    if security_status == "BLOCKED":
        stage_status = "QUARANTINED"
        ai_reason = f"Quarantined: dangerous attachment detected ({', '.join(d['filename'] for d in security_details if not d.get('is_safe', True))})"
    elif category == "SPAM":
        stage_status = "QUARANTINED"
        ai_reason = f"Quarantined: {cls_info.reason}"
    else:
        stage_status = "APPROVED"
        ai_reason = f"Classified as {category} ({cls_info.reason})"

    if batch_info:
        cur_idx, total_count = batch_info
        ai_reason = f"[Batch {cur_idx}/{total_count}] {ai_reason}"

    # 4. Staging row
    staged = StagedEmailRecord(
        stage_id=stage_id,
        source_mailbox=source_mailbox or "imap.inbound@averis.com",
        sender=sender_email or raw_sender,
        recipient=source_mailbox or "sdoc-hub@averis.com",
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

    # 5. Ingest into workflow pipeline ONLY if clean AND category is BL_COMPARISON
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

    # 6. Strict Auto-Reply Gatekeeper
    smtp_result = None
    should_reply = False
    suppression_reason = ""

    if not settings.auto_reply_on_verification:
        suppression_reason = "Auto-reply disabled in settings"
    elif not sender_email:
        suppression_reason = "No sender email address"
    elif security_status != "CLEAN" or stage_status == "QUARANTINED":
        suppression_reason = f"Security suppression: {security_status} / {stage_status} (Silent drop, zero backscatter)"
    elif category == "SPAM":
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
        from app.services.email_utils import (
            build_customer_structured_text,
            build_enterprise_audit_receipt,
            clean_subject,
            extract_original_sender,
        )
        import urllib.parse

        clean_subj = clean_subject(subject)
        orig_client = extract_original_sender(body)
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

        # Construct executive-grade counterpart reply for customer
        client_subj = f"Re: {clean_subj} - {'B/L Document Amendment Required' if is_mismatch else 'Document Verification Complete'}"
        client_reply_text = build_customer_structured_text(
            clean_subj=clean_subj,
            stage_id=stage_id,
            status_desc=status_desc,
            rep=rep,
            target_client=target_client,
            source_mailbox=staged.source_mailbox,
        )
        mailto_link = f"mailto:{target_client}?{urllib.parse.urlencode({'subject': client_subj, 'body': client_reply_text})}"
        # Construct pinpoint Gmail operator search: from:source_email + subject:(keywords)
        safe_subj_kw = re.sub(r'[^\w\s-]', ' ', clean_subj).strip()
        if safe_subj_kw:
            thread_query = f"from:{target_client} subject:({safe_subj_kw})"
        else:
            thread_query = f"from:{target_client}"
        gmail_thread_search = f"https://mail.google.com/mail/u/0/#search/{urllib.parse.quote_plus(thread_query)}"

        public_base = settings.public_base_url or "http://127.0.0.1:8000"
        dispatch_url = (
            f"{public_base.rstrip('/')}/api/v1/gateway/customer-notice/send"
            f"?stage_id={stage_id}&recipient={urllib.parse.quote_plus(target_client)}&auto_send=true"
        )

        reply_body, html_body = build_enterprise_audit_receipt(
            clean_subj=clean_subj,
            stage_id=stage_id,
            status_desc=status_desc,
            rep=rep,
            orig_client=orig_client,
            sender_email=sender_email,
            target_client=target_client,
            client_subj=client_subj,
            client_reply_text=client_reply_text,
            mailto_link=mailto_link,
            gmail_thread_search=gmail_thread_search,
            source_mailbox=staged.source_mailbox,
            dispatch_url=dispatch_url,
        )

        # Check if straight-through penetration dispatch applies:
        # If the document is 100% VERIFIED (OK - 0 discrepancies) and we extracted
        # an original client distinct from the forwarding operator:
        # Directly dispatch the executive-grade customer notice to the client (target_client)
        # and CC the operator (sender_email) for their audit record!
        is_clean_match = bool(rep and rep.status == "OK")
        has_distinct_client = bool(orig_client and sender_email and orig_client.lower() != sender_email.lower())

        if is_clean_match and has_distinct_client:
            from app.services.email_utils import build_customer_html_notice
            dispatch_to = target_client
            dispatch_cc = sender_email
            dispatch_subj = client_subj
            dispatch_body = client_reply_text
            dispatch_html = build_customer_html_notice(
                clean_subj=clean_subj,
                stage_id=stage_id,
                status_desc=status_desc,
                rep=rep,
                target_client=target_client,
                source_mailbox=staged.source_mailbox,
            )
        else:
            dispatch_to = sender_email
            dispatch_cc = None
            dispatch_subj = reply_subj
            dispatch_body = reply_body
            dispatch_html = html_body

        smtp_result = dispatch_smtp_email(
            to_email=dispatch_to,
            subject=dispatch_subj,
            body=dispatch_body,
            html_body=dispatch_html,
            in_reply_to=message_id,
            references=references,
            sender=settings.smtp_from or settings.imap_user,
            cc=dispatch_cc,
        )

        # Persist dispatch record
        rec = DispatchRecord(
            stage_id=stage_id,
            source_mailbox=staged.source_mailbox,
            recipient=dispatch_to,
            decision="MISMATCH" if is_mismatch else "VERIFIED",
            subject=dispatch_subj,
            body=dispatch_body,
            channel=smtp_result.get("channel", "SMTP"),
            delivery=smtp_result.get("delivery", "SIMULATED"),
        )
        db.add(rec)
        staged.status = "RETURNED"
        db.commit()

    return {
        "stage_id": stage_id,
        "sender": sender_email,
        "subject": subject,
        "security_status": security_status,
        "smtp_delivery": smtp_result.get("delivery") if smtp_result else None,
    }


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
        id_list = [i for i in msg_ids[0].split() if i] if (typ == "OK" and msg_ids and msg_ids[0]) else []

        # Resilience against webmail / mobile auto-read:
        # If UNSEEN is empty, inspect recent messages in ALL to catch any incoming email
        # that was auto-marked \Seen by an active web browser or mobile client before this poll cycle.
        processed_keys = _get_processed_keys(db)
        if not id_list:
            typ_all, all_ids = mail.search(None, "ALL")
            if typ_all == "OK" and all_ids and all_ids[0]:
                recent_ids = all_ids[0].split()[-10:]
                for rid in recent_ids:
                    h_res, h_data = mail.fetch(rid, "(BODY.PEEK[HEADER.FIELDS (MESSAGE-ID SUBJECT FROM)])")
                    if h_res == "OK" and h_data and h_data[0]:
                        hdr_msg = email.message_from_bytes(h_data[0][1])
                        h_mid = (hdr_msg.get("Message-ID") or "").strip()
                        h_subj = decode_mime_str(hdr_msg.get("Subject", "")).strip().lower()
                        if (h_mid and h_mid not in processed_keys) and (h_subj and h_subj not in processed_keys):
                            id_list.append(rid)

        if not id_list:
            mail.close()
            mail.logout()
            return {
                "status": "SUCCESS",
                "message": "Connected to IMAP. No new unread messages.",
                "polled_count": 0,
                "processed": [],
            }

        log.info("Found %d candidate emails to process in %s@%s", len(id_list), settings.imap_user, settings.imap_host)

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

            if message_id:
                _PROCESSED_MESSAGE_KEYS.add(message_id.strip())
            if subject:
                _PROCESSED_MESSAGE_KEYS.add(subject.strip().lower())

            nested_emails = find_nested_email_attachments(msg)
            is_batch = len(nested_emails) > 1 or (len(nested_emails) == 1 and not has_direct_document_attachments(msg))
            now_str = dt.datetime.now().strftime("%H%M%S%f")[:9]

            if is_batch:
                total_nested = len(nested_emails)
                log.info(
                    "Batch forward detected: %d nested email(s) from %s. Unbundling...",
                    total_nested,
                    sender_email or raw_sender,
                )
                for idx, (att_name, eml_bytes) in enumerate(nested_emails, 1):
                    child_msg = email.message_from_bytes(eml_bytes)
                    child_subj_raw = child_msg.get("Subject") or att_name
                    child_subject = decode_mime_str(child_subj_raw)
                    child_from_raw = child_msg.get("From", "")
                    child_date = child_msg.get("Date", "")
                    child_body, child_attachments = extract_email_payload(child_msg, ingest_dir)

                    wrapper_sender_str = sender_email or raw_sender
                    header_lines = [
                        f"---------- Forwarded message (Batch {idx} of {total_nested}) ---------",
                    ]
                    if child_from_raw:
                        header_lines.append(f"From: {child_from_raw}")
                    if child_subject:
                        header_lines.append(f"Subject: {child_subject}")
                    if child_date:
                        header_lines.append(f"Date: {child_date}")
                    if wrapper_sender_str:
                        header_lines.append(f"Forwarded-By: {wrapper_sender_str}\n")
                    child_full_body = "\n".join(header_lines) + "\n" + (child_body or "")

                    child_stage_id = f"STG-IMAP-{now_str}-{idx:02d}"
                    child_res = _stage_and_verify_single_email(
                        db=db,
                        stage_id=child_stage_id,
                        subject=child_subject,
                        body=child_full_body,
                        sender_email=wrapper_sender_str,
                        raw_sender=raw_sender,
                        attachments=child_attachments,
                        source_mailbox=settings.imap_user or "imap.inbound@averis.com",
                        settings=settings,
                        batch_info=(idx, total_nested),
                        message_id=message_id,
                        references=references,
                    )
                    processed_stages.append(child_res)
            else:
                body, attachments = extract_email_payload(msg, ingest_dir)
                stage_id = f"STG-IMAP-{now_str}"
                res = _stage_and_verify_single_email(
                    db=db,
                    stage_id=stage_id,
                    subject=subject,
                    body=body,
                    sender_email=sender_email,
                    raw_sender=raw_sender,
                    attachments=attachments,
                    source_mailbox=settings.imap_user or "imap.inbound@averis.com",
                    settings=settings,
                    batch_info=None,
                    message_id=message_id,
                    references=references,
                )
                processed_stages.append(res)

            # Mark as read
            mail.store(num, "+FLAGS", "\\Seen")

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
