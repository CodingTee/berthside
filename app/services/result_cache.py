"""Stored-result cache for the BerthSide integration API.

Rule enforced here: **process once, store the result, reuse the result.**

Every result produced by the existing BerthSide core (classification →
extraction → verification) is written to ``process_results``. The ShipMail
side panel and the Dashboard then read that same row. Opening a page,
refreshing it, switching emails or reopening a shipment never re-runs OCR,
classification, extraction or verification.

This module holds no business logic — it never classifies or verifies anything
itself. It only stores and returns what ``services/workflow`` already produced.
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy.orm import Session

from app.models import ProcessResultRecord

DEFAULT_SOURCE = "shipmail"


# ------------------------------------------------------------------ identity
def content_fingerprint(
    subject: str,
    body: str,
    attachments: list[Any],
) -> str:
    """Stable hash of the email inputs.

    Two calls with identical subject/body/attachments produce the same key, so
    a repeated request is recognised as the same email even when the caller
    forgot to send an ``email_id``. A changed attachment changes the hash,
    which correctly makes a re-run legitimate.
    """
    h = hashlib.sha256()
    h.update((subject or "").encode("utf-8", "replace"))
    h.update(b"\x00")
    h.update((body or "").encode("utf-8", "replace"))
    for att in attachments or []:
        h.update(b"\x00")
        h.update(str(getattr(att, "filename", "") or "").encode("utf-8", "replace"))
        content = getattr(att, "content_text", None) or getattr(
            att, "content_base64", None
        ) or ""
        h.update(b"\x00")
        h.update(str(content).encode("utf-8", "replace"))
    return h.hexdigest()


def attachment_fingerprint(att: Any) -> str:
    content = getattr(att, "content_text", None) or getattr(
        att, "content_base64", None
    ) or ""
    raw = f"{getattr(att, 'filename', '')}\x00{content}"
    return hashlib.sha1(raw.encode("utf-8", "replace")).hexdigest()


def resolve_identity(
    email_id: Optional[str],
    message_id: Optional[str],
    fingerprint: str,
) -> tuple[str, str]:
    """Return ``(email_id, cache_key)`` for a request.

    ``email_id`` falls back to ``message_id`` and then to a content-derived id,
    so a caller that sends neither still gets deterministic dedup instead of a
    fresh random id per request.
    """
    resolved = email_id or message_id or f"MSG_{fingerprint[:12].upper()}"
    cache_key = message_id or email_id or resolved
    return resolved, cache_key


def find_cached(
    db: Session,
    *,
    cache_key: str,
    email_id: Optional[str] = None,
    message_id: Optional[str] = None,
    thread_id: Optional[str] = None,
) -> Optional[ProcessResultRecord]:
    """Look up an existing result by any of its known identifiers."""
    for column, value in (
        (ProcessResultRecord.cache_key, cache_key),
        (ProcessResultRecord.message_id, message_id),
        (ProcessResultRecord.email_id, email_id),
    ):
        if not value:
            continue
        row = db.query(ProcessResultRecord).filter(column == value).first()
        if row is not None:
            return row

    # Same thread, different message: reuse only when we have no per-message
    # identity at all, otherwise a reply would be silently answered with the
    # previous email's result.
    if thread_id and not email_id and not message_id:
        row = (
            db.query(ProcessResultRecord)
            .filter(ProcessResultRecord.thread_id == thread_id)
            .order_by(ProcessResultRecord.id.desc())
            .first()
        )
        if row is not None:
            return row
    return None


# ------------------------------------------------------------------ version
def document_versions(
    db: Session,
    shipment_id: Optional[str],
    fingerprints: dict[str, str],
) -> dict[str, int]:
    """Version number per document type for this shipment.

    ``v1`` the first time a document of that type is seen; +1 whenever a
    *different* revision of it arrives. This is the "Shipping Instruction v3"
    number the side panel shows, derived from stored history — not a guess.
    """
    if not shipment_id or not fingerprints:
        return {doc_type: 1 for doc_type in fingerprints}

    versions: dict[str, int] = {}
    for doc_type, fp in fingerprints.items():
        seen: set[str] = set()
        rows = (
            db.query(ProcessResultRecord.doc_fingerprints)
            .filter(ProcessResultRecord.shipment_id == shipment_id)
            .all()
        )
        for (stored,) in rows:
            if not isinstance(stored, dict):
                continue
            value = stored.get(doc_type)
            if value:
                seen.add(value)
        versions[doc_type] = len(seen) + (0 if fp in seen else 1)
    return versions


# --------------------------------------------------------------------- write
def store_result(
    db: Session,
    *,
    cache_key: str,
    email_id: Optional[str],
    message_id: Optional[str],
    thread_id: Optional[str],
    shipment_id: Optional[str],
    source: str,
    content_hash: str,
    doc_fingerprints: dict[str, str],
    result: dict[str, Any],
) -> ProcessResultRecord:
    """Upsert a processing result (one row per cache_key)."""
    row = db.query(ProcessResultRecord).filter_by(cache_key=cache_key).first()
    if row is None:
        row = ProcessResultRecord(cache_key=cache_key)
        db.add(row)

    row.email_id = email_id
    row.message_id = message_id
    row.thread_id = thread_id
    row.shipment_id = shipment_id
    row.source = source or DEFAULT_SOURCE
    row.content_hash = content_hash
    row.doc_fingerprints = doc_fingerprints
    row.result = result
    row.attempts = (row.attempts or 0) + 1
    row.processing_ms = result.get("processing_ms")
    row.processed_at = datetime.now(timezone.utc)

    db.commit()
    db.refresh(row)
    return row


def cached_response(
    row: ProcessResultRecord, *, reused: bool
) -> dict[str, Any]:
    """Rebuild the API payload from a stored row.

    ``reused`` distinguishes "we just ran the pipeline" from "we returned the
    stored result", which is what lets a UI prove it did not reprocess.
    """
    payload = dict(row.result or {})
    payload["cached"] = reused
    payload["cache_key"] = row.cache_key
    payload["attempts"] = row.attempts or 1
    processed_at = row.processed_at or row.updated_at
    payload["processed_at"] = (
        processed_at.isoformat() if isinstance(processed_at, datetime) else None
    )
    if reused:
        # A reused result costs no pipeline time. Reporting the original
        # duration would misrepresent this response.
        payload["processing_ms"] = 0.0
    return payload


def list_results(db: Session, limit: int = 50) -> list[dict[str, Any]]:
    """Stored results, newest first — what the Dashboard renders."""
    rows = (
        db.query(ProcessResultRecord)
        .order_by(ProcessResultRecord.id.desc())
        .limit(limit)
        .all()
    )
    return [cached_response(row, reused=True) for row in rows]


def count_results(db: Session) -> int:
    return db.query(ProcessResultRecord).count()
