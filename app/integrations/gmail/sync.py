"""Poll real Gmail and hand messages to BerthSide Core."""
from __future__ import annotations

import base64
import datetime as dt
import logging
import re
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from sqlalchemy.orm import Session

from app.config import BACKEND_ROOT, get_settings
from app.integrations.gmail import client, parser
from app.models import (
    EmailRecord,
    GmailMessageRecord,
    OPS_MAILBOX,
    ReportRecord,
    infer_source_mailbox,
    utcnow,
)
from app.routers.ingest import (_decode_attachment, _portable_path, _safe_segment,
                                expand_payloads)
from app.services import doc_types, result_cache, workflow
from app.services.security import verify_file_safety

log = logging.getLogger(__name__)

# Sorts an undated message before any dated one while keeping its arrival
# position, so a batch is never silently reshuffled by a missing timestamp.
_EPOCH = dt.datetime(1970, 1, 1)

# Real senders frequently paste the Shipping Instruction / Bill of Lading as
# plain text in the email body instead of attaching a file, and they usually
# send SI and BL as two separate emails. The core pipeline is attachment-based
# and compares within one email, so the Gmail layer bridges both habits:
#   1. an inline SI/BL body becomes a fallback attachment only when no real
#      attachment covers that document kind;
#   2. when an email holds only one half of the pair, the counterpart saved from
#      a previously synced email of the same shipment is attached too.
# Document kinds are decided by `app.services.doc_types` — the engine's own
# rules — so this layer and the pipeline can never disagree about what an SI is.
# Everything happens here; the scored pipeline and its datasets are untouched.
_INLINE_SI_RE = re.compile(r"\bshipping\s+instruction\b", re.IGNORECASE)
_INLINE_BL_RE = re.compile(r"\bbill\s+of\s+lading\b", re.IGNORECASE)
_PAIR_TOKEN_RE = re.compile(r"([A-Za-z0-9]+-[A-Za-z0-9]+)_(SI|BL)[._]",
                            re.IGNORECASE)
_SHIPMENT_RE = re.compile(r"\b([A-Z]{2,6}-\d{2,6})\b")


def poll_and_process(db: Session, limit: int | None = None) -> dict[str, Any]:
    """Fetch unseen Gmail messages and process them through BerthSide workflow."""
    started = time.perf_counter()
    message_ids = client.list_message_ids(max_results=limit)
    processed = skipped = failed = 0
    results = []

    # Chronological, oldest first — deliberately not Gmail's return order.
    #
    # Gmail lists newest first, and processing in that order meant a shipment's
    # documents were filed newest-first: the first-issued Shipping Instruction
    # became the last one registered, and "last registered is current" then
    # nominated the oldest document as the current one. Ordering the batch here
    # means the pipeline sees the mail in the order a human would read it, which
    # also lets an invoice arriving after its SI find the shipment it belongs to.
    #
    # Message ids are fetched and parsed before processing so the whole batch can
    # be sorted by the timestamp Gmail reports for each message; the per-message
    # work is the same as before, just reordered.
    pending = _fetch_pending(db, message_ids)
    for message_id, raw, payload in pending:
        try:
            result = process_parsed_payload(db, raw, payload)
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
    skipped = len(message_ids) - len(pending)

    # Second pass — SI and BL usually arrive as two separate emails, so the
    # second half of a pair can still be processed before the first half is
    # stored (equal or missing timestamps, or older mail that only became
    # visible now). Any message still holding only one half gets one more
    # attempt once its counterpart exists on disk.
    for extra in _reprocess_completed_pairs(db, message_ids):
        results.append(extra)
        processed += 1

    return {
        "requested": len(message_ids),
        "processed": processed,
        "skipped": skipped,
        "failed": failed,
        "elapsed_ms": (time.perf_counter() - started) * 1000.0,
        "results": results,
    }


def _fetch_pending(db: Session, message_ids: list[str]) -> list[tuple[str, dict, Any]]:
    """Parse the unseen messages and return them oldest-first.

    ``received_at`` comes from the mail's own ``Date`` header (see
    `parser.parse_message`), falling back to Gmail's ``internalDate``. A message
    with no usable timestamp keeps its position from the API — a stable sort, so
    unsorted mail is not reshuffled.
    """
    fetched: list[tuple[str, dict, Any, Any]] = []
    for index, message_id in enumerate(message_ids):
        existing = db.query(GmailMessageRecord).filter_by(
            gmail_message_id=message_id).first()
        if existing and existing.processing_status == "PROCESSED":
            continue
        try:
            raw = client.get_message(message_id)
            payload = parser.parse_message(raw, client.get_attachment)
        except Exception as exc:  # noqa: BLE001 - reported per message below
            log.warning("could not fetch Gmail message %s: %s", message_id, exc)
            continue
        fetched.append((message_id, raw, payload, index))

    def sort_key(item):
        raw = item[1]
        stamp = _received_at(item[2], raw)
        # No timestamp: keep the position the API gave us rather than inventing
        # one. `datetime.max` would push undated mail to the end, which is just
        # as arbitrary but less predictable.
        return (stamp is not None, stamp or _EPOCH, item[3])

    fetched.sort(key=sort_key)
    return [(mid, raw, payload) for mid, raw, payload, _ in fetched]


def _reprocess_completed_pairs(db: Session, message_ids: list[str]) -> list[dict[str, Any]]:
    """Re-run messages whose missing half arrived while the batch ran."""
    extra: list[dict[str, Any]] = []
    for message_id in message_ids:
        record = db.query(GmailMessageRecord).filter_by(gmail_message_id=message_id).first()
        if not record or record.processing_status != "PROCESSED":
            continue
        email = db.query(EmailRecord).filter_by(email_id=record.email_id).first()
        if not email or not email.attachments:
            continue
        if len(_attachment_kinds(list(email.attachments))) != 1:
            continue  # already a complete pair (or no docs at all)
        if _attach_counterpart_document(
                record.subject, email.body,
                list(email.attachments)) == list(email.attachments):
            continue  # counterpart still not on disk
        record.processing_status = "PENDING"
        db.commit()
        try:
            extra.append(process_message_id(db, message_id))
        except Exception as exc:  # noqa: BLE001
            record.processing_status = "ERROR"
            record.error_message = f"{type(exc).__name__}: {exc}"
            db.commit()
    return extra


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
    attachments = _materialize_inline_documents(
        email_id, payload.subject, payload.body, attachments
    )
    attachments = _attach_counterpart_document(
        payload.subject, payload.body, attachments)
    email = db.query(EmailRecord).filter_by(email_id=email_id).first()
    if email is None:
        email = EmailRecord(email_id=email_id)
        db.add(email)
    if not email.source_mailbox:
        email.source_mailbox = OPS_MAILBOX
    email.sender = payload.sender
    email.subject = payload.subject
    email.body = payload.body
    email.attachments = attachments
    # When the mail was actually received. This is what version chronology is
    # built on, so it must be stored before the pipeline runs: without it the
    # only ordering signal left is the order Gmail happened to return messages
    # in, which is newest-first and would invert the version numbers.
    received = _received_at(payload, raw_message)
    if received is not None:
        email.received_at = received

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
    # Mirror the run into the process-once cache so the ShipMail side panel and
    # the Dashboard read the SAME stored result instead of a stale manual run.
    _cache_sync_result(
        db, email_id=email_id, message_id=message_id,
        thread_id=record.thread_id, subject=payload.subject,
        body=payload.body, attachment_paths=attachments, report=report,
    )
    # shipment_key ("REF:SHP-001") is the human-readable code; extracted's
    # raw shipment_id is an internal DB row number.
    extracted = report.extracted or {}
    ship_key = str(extracted.get("shipment_key") or "")
    return {
        "gmail_message_id": message_id,
        "email_id": email_id,
        "thread_id": record.thread_id,
        "status": report.status,
        "category": report.category,
        "has_defect": bool(report.has_defect),
        "defect_fields": report.defect_fields or [],
        "review_reason": report.review_reason,
        "shipment_id": ship_key.split(":")[-1] or None,
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


def _attachment_kinds(attachments: list[str]) -> set[str]:
    """Document kinds (SI/BL) among the attachments, per the shared rules."""
    return {
        kind for kind in (doc_types.detect(str(a)) for a in attachments)
        if kind in ("SI", "BL")
    }


def _received_at(payload, raw_message: dict[str, Any]):
    """When the message was received, from the strongest available source.

    Order of preference:

    1. the ``Date`` header, parsed by the Gmail parser (the mail's own claim);
    2. Gmail's ``internalDate`` (epoch milliseconds), which the server sets and
       cannot be spoofed or omitted by the sender.

    Returns ``None`` when neither is usable. Callers must treat that as "no
    chronology available" and fall back to registration order — an unusable
    timestamp must never make ingestion fail.
    """
    received = (payload.metadata or {}).get("received")
    if received:
        try:
            value = dt.datetime.fromisoformat(str(received))
            # SQLite stores naive datetimes; a mixed naive/aware column makes
            # every later comparison raise. Normalise to naive UTC.
            return (value.astimezone(dt.timezone.utc).replace(tzinfo=None)
                    if value.tzinfo else value)
        except ValueError:
            pass

    internal = raw_message.get("internalDate")
    try:
        if internal is not None:
            return dt.datetime.fromtimestamp(
                int(internal) / 1000.0, tz=dt.timezone.utc).replace(tzinfo=None)
    except (TypeError, ValueError, OSError):
        pass
    return None


def _shipment_code(subject: str | None, body: str | None) -> str | None:
    """The shipment reference a human wrote in the mail, if any."""
    match = _SHIPMENT_RE.search(f"{subject or ''}\n{body or ''}")
    return match.group(1).upper() if match else None


def _materialize_inline_documents(
    email_id: str, subject: str | None, body: str | None, attachments: list[str]
) -> list[str]:
    """Turn an inline SI/BL email body into a fallback file the core can read.

    Two guards, in this order:

    1. **A real attachment always wins.** The body is only used for a document
       kind that no real attachment already provides. Before this, an email
       carrying `SI_v1.pdf` also produced a `SI` document out of its own body,
       and the engine — which prefers the `_SI.` naming convention — then read
       the body instead of the PDF, so the attached document was never looked
       at.
    2. The text must name exactly one kind; a body that mentions both is left
       alone rather than mis-split.

    The synthesised file is named `<shipment>_<kind>.frombody.txt` so it is
    recognisable as a fallback and can never outrank a real file downstream.
    """
    if not body:
        return attachments

    named = []
    if _INLINE_SI_RE.search(subject or "") or _INLINE_SI_RE.search(body):
        named.append("SI")
    if _INLINE_BL_RE.search(subject or "") or _INLINE_BL_RE.search(body):
        named.append("BL")

    # Only the kinds the text names AND no real attachment covers.
    already_attached = _attachment_kinds(attachments)
    to_materialize = [k for k in named if k not in already_attached]
    if len(to_materialize) != 1:
        return attachments

    shipment = _safe_segment(_shipment_code(subject, body) or "UNKNOWN", "shipment")
    target_dir = Path(get_settings().ingest_dir) / "gmail" / _safe_segment(email_id, "gmail")
    target_dir.mkdir(parents=True, exist_ok=True)
    path = target_dir / f"{shipment}_{to_materialize[0]}{doc_types.SYNTHETIC_SUFFIX}.txt"
    text = body
    if subject and subject.lower() not in body.lower():
        text = f"{subject}\n\n{body}"
    path.write_text(text, encoding="utf-8")
    return attachments + [_portable_path(path)]


def _attach_counterpart_document(
    subject: str | None, body: str | None, attachments: list[str]
) -> list[str]:
    """Complete an SI/BL pair across separately-sent emails.

    SI and BL usually arrive as two emails, and the engine compares within one
    email. When this email holds exactly one half of the pair for a shipment and
    a previously synced email already stored the other half, that half is
    attached so the comparison can run.

    The index is the **shipment reference written in the mail** (subject/body),
    falling back to one embedded in the attachment names. It used to require the
    literal `_SI.`/`_BL.` filename convention, which meant the pairing silently
    stopped working the moment a sender named a file `SI_v1.pdf`.
    """
    kinds = _attachment_kinds(attachments)
    if len(kinds) != 1:
        return attachments
    shipment = _shipment_code(subject, body) or next(
        (m.group(1).upper() for a in attachments
         if (m := _PAIR_TOKEN_RE.search(str(a)))), None)
    if not shipment:
        return attachments
    other = "BL" if "SI" in kinds else "SI"
    gmail_dir = Path(get_settings().ingest_dir) / "gmail"
    if not gmail_dir.is_dir():
        return attachments
    for path in sorted(gmail_dir.glob(f"*/{shipment}_{other}*")):
        portable = _portable_path(path)
        if portable not in attachments:
            return attachments + [portable]
    return attachments


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
    """Mirror a sync run into ``process_results`` (the process-once cache).

    The side panel reads ``GET /api/results/{email_id}``. Without this mirror
    it would keep showing the result of an older manual "Run BerthSide" click
    instead of what the Gmail sync just decided. Best-effort: a cache failure
    must never fail the sync itself.
    """
    try:
        atts: list[SimpleNamespace] = []
        for portable in attachment_paths:
            path = Path(portable)
            if not path.is_file():
                path = BACKEND_ROOT / portable
            if not path.is_file():
                continue
            atts.append(SimpleNamespace(
                filename=path.name,
                content_text=path.read_text(encoding="utf-8", errors="replace"),
            ))

        fingerprint = result_cache.content_fingerprint(subject, body, atts)
        _, cache_key = result_cache.resolve_identity(email_id, message_id, fingerprint)

        # Kinds are decided by the shared detector, so the side panel shows the
        # same documents the engine actually read. This used to require the
        # literal `_SI.`/`_BL.` convention: with a name like `SI_v1.pdf` the
        # panel reported the pair as missing while the engine was reading it.
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
            # ReportRecord does not persist the classifier confidence; the
            # schema requires a float, and the panel never displays it.
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
            "source": "gmail",
        }
        result_cache.store_result(
            db,
            cache_key=cache_key,
            email_id=email_id,
            message_id=message_id,
            thread_id=thread_id,
            shipment_id=shipment_code,
            source="gmail",
            content_hash=fingerprint,
            doc_fingerprints=doc_fingerprints,
            result=result,
        )
    except Exception:  # noqa: BLE001 — cache mirror is best-effort
        import logging

        logging.getLogger(__name__).exception(
            "Failed to mirror Gmail sync result for %s", email_id
        )
