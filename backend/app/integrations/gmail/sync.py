"""Poll real Gmail and hand messages to ShipSync Core."""
from __future__ import annotations

import base64
import time
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from app.config import BACKEND_ROOT, get_settings
from app.integrations.gmail import client, parser
from app.models import EmailRecord, GmailMessageRecord, ReportRecord, utcnow
from app.routers.ingest import _decode_attachment, _portable_path, _safe_segment
from app.services import workflow
from app.services.security import verify_file_safety


def poll_and_process(db: Session, limit: int | None = None) -> dict[str, Any]:
    """Fetch unseen Gmail messages and process them through ShipSync workflow."""
    started = time.perf_counter()
    message_ids = client.list_message_ids(max_results=limit)
    processed = skipped = failed = 0
    results = []
    for message_id in message_ids:
        existing = db.query(GmailMessageRecord).filter_by(gmail_message_id=message_id).first()
        if existing and existing.processing_status == "PROCESSED":
            skipped += 1
            continue
        try:
            result = process_message_id(db, message_id)
            processed += 1
            results.append(result)
        except Exception as exc:  # noqa: BLE001
            failed += 1
            record = _get_or_create_gmail_record(db, message_id)
            record.processing_status = "ERROR"
            record.error_message = f"{type(exc).__name__}: {exc}"
            db.commit()
            results.append({
                "gmail_message_id": message_id,
                "status": "ERROR",
                "error": record.error_message,
            })
    return {
        "requested": len(message_ids),
        "processed": processed,
        "skipped": skipped,
        "failed": failed,
        "elapsed_ms": (time.perf_counter() - started) * 1000.0,
        "results": results,
    }


def process_message_id(db: Session, message_id: str) -> dict[str, Any]:
    raw = client.get_message(message_id)
    payload = parser.parse_message(raw, client.get_attachment)
    return process_parsed_payload(db, raw, payload)


def process_parsed_payload(
    db: Session, raw_message: dict[str, Any], payload
) -> dict[str, Any]:
    """Persist parsed Gmail email and run the existing stateful workflow."""
    message_id = raw_message.get("id") or payload.metadata.get("gmail_message_id")
    record = _get_or_create_gmail_record(db, message_id)
    if record.processing_status == "PROCESSED":
        report = db.query(ReportRecord).filter_by(email_id=record.email_id).first()
        return {
            "gmail_message_id": message_id,
            "email_id": record.email_id,
            "status": report.status if report else "PROCESSED",
            "skipped": True,
        }

    email_id = payload.email_id or f"GMAIL-{message_id}"
    attachments = _persist_gmail_attachments(email_id, payload.attachments)
    email = db.query(EmailRecord).filter_by(email_id=email_id).first()
    if email is None:
        email = EmailRecord(email_id=email_id)
        db.add(email)
    email.sender = payload.sender
    email.subject = payload.subject
    email.body = payload.body
    email.attachments = attachments

    record.gmail_message_id = message_id
    record.thread_id = raw_message.get("threadId") or payload.metadata.get("thread_id")
    record.history_id = raw_message.get("historyId") or payload.metadata.get("history_id")
    record.email_id = email_id
    record.sender = payload.sender
    record.subject = payload.subject
    record.processing_status = "PROCESSING"
    db.commit()

    report = workflow.process_email(db, email_id)
    record.processing_status = "PROCESSED" if report.status != "ERROR" else "ERROR"
    record.processed_at = utcnow()
    record.error_message = report.error_message
    db.commit()
    return {
        "gmail_message_id": message_id,
        "email_id": email_id,
        "thread_id": record.thread_id,
        "status": report.status,
        "category": report.category,
        "has_defect": bool(report.has_defect),
        "defect_fields": report.defect_fields or [],
        "review_reason": report.review_reason,
        "shipment_id": (report.extracted or {}).get("shipment_id"),
    }


def _get_or_create_gmail_record(db: Session, message_id: str) -> GmailMessageRecord:
    record = db.query(GmailMessageRecord).filter_by(gmail_message_id=message_id).first()
    if record:
        return record
    record = GmailMessageRecord(
        gmail_message_id=message_id,
        email_id=f"GMAIL-{message_id}",
    )
    db.add(record)
    db.flush()
    return record


def _persist_gmail_attachments(email_id: str, attachments) -> list[str]:
    settings = get_settings()
    target_dir = Path(settings.ingest_dir) / "gmail" / _safe_segment(email_id, "gmail")
    target_dir.mkdir(parents=True, exist_ok=True)
    paths: list[str] = []
    for att in attachments:
        content = _decode_attachment(att)
        safe, reason = verify_file_safety(att.filename, content)
        if not safe:
            # Preserve a text evidence file instead of feeding unsafe bytes into
            # the document readers. The report will still show the email.
            evidence_name = _safe_segment(att.filename, "blocked_attachment") + ".blocked.txt"
            path = target_dir / evidence_name
            path.write_text(f"Blocked Gmail attachment: {reason}", encoding="utf-8")
            paths.append(_portable_path(path))
            continue
        path = target_dir / _safe_segment(att.filename, "attachment")
        path.write_bytes(content)
        paths.append(_portable_path(path))
    return paths
