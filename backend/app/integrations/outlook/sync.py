"""Sync Outlook inbox messages through Microsoft Graph API into SDOC pipeline."""
from __future__ import annotations

import datetime as dt
import hashlib
import logging
import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any

try:
    from bs4 import BeautifulSoup
except ImportError:  # optional: HTML mail falls back to raw text
    BeautifulSoup = None
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import EmailRecord, GmailMessageRecord, ReportRecord
from app.routers.ingest import _safe_segment
from app.services import doc_types, result_cache, workflow
from app.services.security import verify_file_safety
from app.integrations.outlook import client

def utcnow():
    return dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)

log = logging.getLogger(__name__)

OPS_MAILBOX = "operations@shipsync.demo"
_PAIR_TOKEN_RE = re.compile(r"(?:^|[\W_])([A-Za-z0-9]{3,4}[-_]?[0-9]{3,6})(?:[\W_]|$)", re.IGNORECASE)
_INLINE_SI_RE = re.compile(r"shipping[\s_-]?instruction|\bsi\b", re.IGNORECASE)
_INLINE_BL_RE = re.compile(r"bill[\s_-]?of[\s_-]?lading|draft[\s_-]?b[\/_]?l|\bb[\/_]l\b|\bbl\b", re.IGNORECASE)


def _clean_body(body_dict: dict[str, Any] | str) -> str:
    if isinstance(body_dict, str):
        raw = body_dict
    elif isinstance(body_dict, dict):
        raw = body_dict.get("content", "")
    else:
        raw = ""
    if BeautifulSoup and ("<html" in raw.lower() or "<body" in raw.lower() or "<p" in raw.lower() or "<div" in raw.lower()):
        try:
            soup = BeautifulSoup(raw, "html.parser")
            return soup.get_text(separator="\n").strip()
        except Exception:
            return raw.strip()
    return raw.strip()


def _portable_path(path: Path) -> str:
    parts = path.resolve().parts
    if "data" in parts:
        idx = parts.index("data")
        return "/".join(parts[idx:])
    return str(path).replace("\\", "/")


def _attachment_kinds(attachments: list[str]) -> set[str]:
    return {
        kind for kind in (doc_types.detect(str(a)) for a in attachments)
        if kind in ("SI", "BL")
    }


def _materialize_inline_documents(
    email_id: str,
    subject: str | None,
    body: str | None,
    attachments: list[str],
) -> list[str]:
    if not body:
        return attachments
    named = []
    if _INLINE_SI_RE.search(subject or "") or _INLINE_SI_RE.search(body):
        named.append("SI")
    if _INLINE_BL_RE.search(subject or "") or _INLINE_BL_RE.search(body):
        named.append("BL")

    already_attached = _attachment_kinds(attachments)
    to_materialize = [k for k in named if k not in already_attached]
    if len(to_materialize) != 1:
        return attachments

    target_dir = Path(get_settings().ingest_dir) / "outlook" / _safe_segment(email_id, "outlook")
    target_dir.mkdir(parents=True, exist_ok=True)
    path = target_dir / f"SHP_{to_materialize[0]}{doc_types.SYNTHETIC_SUFFIX}.txt"
    text = body
    if subject and subject.lower() not in body.lower():
        text = f"{subject}\n\n{body}"
    path.write_text(text, encoding="utf-8")
    return attachments + [_portable_path(path)]


def _attach_counterpart_document(
    subject: str | None, body: str | None, attachments: list[str]
) -> list[str]:
    kinds = _attachment_kinds(attachments)
    if len(kinds) != 1:
        return attachments
    shipment_match = _PAIR_TOKEN_RE.search(f"{subject or ''} {body or ''}")
    shipment = shipment_match.group(1).upper() if shipment_match else None
    if not shipment:
        return attachments
    other = "BL" if "SI" in kinds else "SI"
    ingest_dir = Path(get_settings().ingest_dir)
    for folder in [ingest_dir / "outlook", ingest_dir / "gmail"]:
        if folder.is_dir():
            for path in sorted(folder.glob(f"*/{shipment}_{other}*")):
                portable = _portable_path(path)
                if portable not in attachments:
                    return attachments + [portable]
    return attachments


def poll_and_process(db: Session, limit: int = 20) -> dict[str, Any]:
    """Pull latest messages from Outlook inbox and run through ShipSync workflow."""
    messages = client.list_messages(limit=limit)
    if not messages:
        return {"polled_count": 0, "processed": []}

    settings = get_settings()
    processed_items = []

    for msg in messages:
        raw_id = msg.get("id", "")
        if not raw_id:
            continue
        # Standardize email_id deterministically using sha256 of full message ID
        msg_hash = hashlib.sha256(raw_id.encode("utf-8")).hexdigest()[:16]
        email_id = f"OUTLOOK-{msg_hash}"
        subject = msg.get("subject", "(no subject)")
        body = _clean_body(msg.get("body") or msg.get("bodyPreview") or "")

        from_dict = (msg.get("from") or {}).get("emailAddress") or {}
        sender_name = from_dict.get("name", "")
        sender_email = from_dict.get("address", "")
        sender_str = f"{sender_name} <{sender_email}>" if sender_name and sender_email else (sender_email or sender_name or "unknown")

        rec_time_str = msg.get("receivedDateTime")
        received_at = None
        if rec_time_str:
            try:
                received_at = dt.datetime.fromisoformat(rec_time_str.replace("Z", "+00:00")).astimezone(dt.timezone.utc).replace(tzinfo=None)
            except Exception:
                pass

        # Check existing
        existing_email = db.query(EmailRecord).filter_by(email_id=email_id).first()
        if existing_email and db.query(ReportRecord).filter_by(email_id=email_id).first():
            continue

        # Fetch attachments
        attachments_paths: list[str] = []
        if msg.get("hasAttachments"):
            raw_atts = client.get_attachments(raw_id)
            target_dir = Path(settings.ingest_dir) / "outlook" / _safe_segment(email_id, "outlook")
            target_dir.mkdir(parents=True, exist_ok=True)
            for att in raw_atts:
                fname = att.get("name", "attachment")
                content = att.get("contentBytes", b"")
                safe, reason = verify_file_safety(fname, content)
                if not safe:
                    ev_path = target_dir / f"{_safe_segment(fname, 'blocked')}.blocked.txt"
                    ev_path.write_text(f"Blocked Outlook attachment: {reason}", encoding="utf-8")
                    attachments_paths.append(_portable_path(ev_path))
                    continue
                p = target_dir / _safe_segment(fname, "attachment")
                p.write_bytes(content)
                attachments_paths.append(_portable_path(p))

        # Inline body document and counterpart pairing
        attachments_paths = _materialize_inline_documents(email_id, subject, body, attachments_paths)
        attachments_paths = _attach_counterpart_document(subject, body, attachments_paths)

        # Create or update EmailRecord
        if not existing_email:
            email_rec = EmailRecord(
                email_id=email_id,
                sender=sender_str,
                subject=subject,
                body=body,
                attachments=attachments_paths,
                received_at=received_at or utcnow(),
                source_mailbox=f"outlook:{sender_email}" if sender_email else "outlook",
            )
            db.add(email_rec)
        else:
            email_rec = existing_email
            email_rec.sender = sender_str
            email_rec.subject = subject
            email_rec.body = body
            email_rec.attachments = attachments_paths

        db.commit()

        # Run SDOC workflow engine (classification, extraction, 7-field comparison)
        report = workflow.process_email(db, email_id)
        db.commit()

        # Cache result for instant side-panel access
        _cache_sync_result(
            db,
            email_id=email_id,
            message_id=f"OUTLOOK-{msg_hash}",
            thread_id=None,
            subject=subject,
            body=body,
            attachment_paths=attachments_paths,
            report=report,
        )

        processed_items.append({
            "email_id": email_id,
            "subject": subject,
            "status": report.status,
            "category": report.category,
        })

    return {"polled_count": len(processed_items), "processed": processed_items}


def _cache_sync_result(
    db: Session,
    *,
    email_id: str,
    message_id: str | None,
    thread_id: str | None,
    subject: str | None,
    body: str | None,
    attachment_paths: list[str],
    report: ReportRecord,
) -> None:
    try:
        backend_root = Path(__file__).resolve().parents[3]
        atts: list[SimpleNamespace] = []
        for portable in attachment_paths:
            path = Path(portable)
            if not path.is_file():
                path = backend_root / portable
            if not path.is_file():
                continue
            atts.append(SimpleNamespace(
                filename=path.name,
                content_text=path.read_text(encoding="utf-8", errors="replace"),
            ))

        fingerprint = result_cache.content_fingerprint(subject, body, atts)
        _, cache_key = result_cache.resolve_identity(email_id, message_id, fingerprint)

        found_kinds: list[str] = []
        doc_fingerprints: dict[str, str] = {}
        received_names: dict[str, str] = {}
        for att in atts:
            kind = doc_types.detect(att.filename)
            if kind not in ("SI", "BL"):
                continue
            if kind not in found_kinds:
                found_kinds.append(kind)
            doc_fingerprints[kind] = result_cache.attachment_fingerprint(att)
            received_names.setdefault(kind, att.filename)

        missing = ([d for d in ("SI", "BL") if d not in found_kinds]
                   if report.category == "BL_COMPARISON" else [])
        documents = (
            [{"type": d, "filename": received_names.get(d),
              "status": "received", "version": None}
             for d in found_kinds]
            + [{"type": d, "filename": None, "status": "missing", "version": None}
               for d in missing]
        )
        field_results = [
            fr for fr in (report.field_results or []) if isinstance(fr, dict)
        ]
        extracted_fields = {
            fr.get("field"): {"si": fr.get("si_value"), "bl": fr.get("bl_value")}
            for fr in field_results if fr.get("field")
        }
        action = {
            "OK": "RELEASE_BL",
            "MISMATCH": "HOLD_BL_RELEASE",
            "NEEDS_REVIEW": "HUMAN_REVIEW",
        }.get(report.status,
              "ROUTE_TO_SI_REQUEST" if report.category == "SI_REQUEST"
              else "NO_ACTION")

        shipment_code = next(iter(doc_fingerprints), None)
        shipment_match = _PAIR_TOKEN_RE.search(str(attachment_paths[0] if attachment_paths else ""))
        if shipment_match:
            shipment_code = shipment_match.group(1)

        result = {
            "email_id": email_id,
            "status": report.status,
            "category": report.category,
            "confidence": 0.0,
            "classification_reason": None,
            "suggested_action": action,
            "shipment_id": shipment_code,
            "documents": documents,
            "missing_documents": missing,
            "extracted_fields": extracted_fields,
            "defect_fields": report.defect_fields or [],
            "possible_fields": [],
            "field_results": field_results,
            "has_defect": bool(report.has_defect),
            "review_reason": report.review_reason,
            "security_alerts": [],
            "action": None,
            "verification": {
                "status": report.status,
                "has_defect": bool(report.has_defect),
                "defect_fields": report.defect_fields or [],
                "possible_fields": [],
                "field_results": field_results,
                "review_reason": report.review_reason,
                "missing_documents": missing,
            },
            "actions": [],
            "cached": False,
            "processing_ms": float(report.processing_ms or 0.0),
            "source": "outlook",
        }
        result_cache.store_result(
            db,
            cache_key=cache_key,
            email_id=email_id,
            message_id=message_id,
            thread_id=thread_id,
            shipment_id=shipment_code,
            source="outlook",
            content_hash=fingerprint,
            doc_fingerprints=doc_fingerprints,
            result=result,
        )
    except Exception as e:
        log.warning("Cache sync error for %s: %s", email_id, e)

