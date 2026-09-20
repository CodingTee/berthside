"""External Ingestion & Verification Router.

Exposes RESTful endpoints for third-party systems (RPA, mail gateways, CRM/ERP)
to send emails into SDOC for intelligent classification, document extraction,
and deterministic discrepancy checks.

Endpoints:
- POST /api/v1/analyze : Stateless immediate verification (in-memory, no DB write).
- POST /api/v1/ingest  : Stateful ingestion (saves to DB, syncs to Review Desk UI).
"""
from __future__ import annotations

import base64
import logging
import re
import time
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.config import BACKEND_ROOT, get_settings
from app.database import get_db
from app.models import EmailRecord, ReportRecord
from app.schemas import (
    AttachmentPayload,
    EmailAnalyzeRequest,
    EmailAnalyzeResponse,
    EmailIngestResponse,
    FieldResult,
)
from app.services import workflow
from app.services.security import (MAX_ATTACHMENT_SIZE, encoded_size_exceeds_cap,
                                   verify_file_safety)

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1", tags=["ingest-analyze"])

# Maps a review_reason to the operator action an external caller should take,
# plus the key on the verdict's evidence naming the affected documents.
_ESCALATION_ACTIONS = {
    "missing_attachment": ("REQUEST_MISSING_DOCUMENTS", "missing_documents"),
    "unreadable": ("MANUAL_REVIEW_UNREADABLE", "unreadable_documents"),
}


def _decode_attachment(att: AttachmentPayload) -> bytes:
    """Extract raw bytes from base64 or text attachment payload."""
    if att.content_base64:
        try:
            return base64.b64decode(att.content_base64)
        except Exception as exc:
            raise HTTPException(400, f"Invalid base64 payload for '{att.filename}': {exc}")
    if att.content_text is not None:
        return att.content_text.encode("utf-8")
    return b""


def _suggested_action(verdict: workflow.EmailVerdict) -> str:
    """Turn a verdict into the action an external caller should take next."""
    if verdict.status == "SKIPPED":
        return f"ROUTE_TO_{verdict.category}"
    if verdict.status == "MISMATCH":
        return f"NOTIFY_FORWARDER_AMENDMENT: {', '.join(verdict.defect_fields)}"
    if verdict.status == "NEEDS_REVIEW":
        action = _ESCALATION_ACTIONS.get(verdict.review_reason)
        if action is None:
            return f"ESCALATE_TO_REVIEW_DESK: {verdict.review_reason or 'undecidable'}"
        verb, evidence_key = action
        documents = verdict.extracted.get(evidence_key) or []
        return f"{verb}: {', '.join(documents)}"
    return "AUTO_APPROVE"


def _portable_path(path: Path) -> str:
    """Store a path relative to the backend root when it lives under it.

    Attachment paths go into the database and are resolved against the backend
    root by `inbox_service`, so a relative path keeps the database portable
    across machines and containers. A test that redirects INGEST_DIR outside
    the repo cannot be expressed relative to it, so it keeps the absolute one.
    """
    try:
        return path.relative_to(BACKEND_ROOT).as_posix()
    except ValueError:
        return str(path)


_UNSAFE_SEGMENT_CHARS = re.compile(r"[^A-Za-z0-9._-]+")


def _safe_segment(value: str, fallback: str) -> str:
    """Reduce untrusted text to one safe path segment.

    Both the caller-supplied email_id and the attachment filename become path
    components under INGEST_DIR, so a value like "../../x" must not survive.
    This endpoint is externally reachable, so neither is assumed well behaved.
    """
    name = Path(value).name
    cleaned = _UNSAFE_SEGMENT_CHARS.sub("_", name).strip("._")
    return cleaned[:120] or fallback


@router.post("/analyze", response_model=EmailAnalyzeResponse, summary="Stateless instant email verification")
def analyze_email(payload: EmailAnalyzeRequest) -> EmailAnalyzeResponse:
    """Stateless analysis endpoint.

    Takes email metadata + attachments, runs the safety gate, then hands the
    email to the same evaluator the corpus pipeline uses, and returns the
    verdict in milliseconds. Zero database persistence.
    """
    t0 = time.perf_counter()
    email_id = payload.email_id or f"ANL_{uuid.uuid4().hex[:8].upper()}"

    # 1. Decode and verify attachments
    decoded: dict[str, bytes] = {}
    security_alerts: list[str] = []

    for att in payload.attachments:
        # Reject an oversized payload *before* decoding it. The cap is about
        # not letting one request exhaust the process, and a payload that is
        # rejected only after it has been fully decoded has already spent the
        # memory the cap is there to protect.
        if encoded_size_exceeds_cap(att.content_base64, att.content_text):
            security_alerts.append(
                f"{att.filename}: exceeds the "
                f"{MAX_ATTACHMENT_SIZE // (1024 * 1024)}MB attachment limit")
            continue
        content = _decode_attachment(att)
        is_safe, reason = verify_file_safety(att.filename, content)
        if not is_safe:
            security_alerts.append(f"{att.filename}: {reason}")
        decoded[att.filename] = content

    # If any attachment is flagged as dangerous executable/malicious
    if security_alerts:
        elapsed = (time.perf_counter() - t0) * 1000.0
        return EmailAnalyzeResponse(
            email_id=email_id,
            category="SPAM",
            confidence=1.0,
            classification_reason="Dangerous or spoofed attachment detected by security gate",
            status="ERROR",
            has_defect=False,
            defect_fields=[],
            field_results=[],
            review_reason="security_quarantine",
            suggested_action="QUARANTINE_AND_ALERT_SECURITY",
            security_alerts=security_alerts,
            processing_ms=elapsed,
        )

    # 2. Hand the email to the shared evaluator -----------------------------
    # The verification rules live in workflow.evaluate_email. Calling them
    # instead of restating them here is what keeps this endpoint and the
    # corpus pipeline from drifting apart: this file used to carry its own copy
    # and silently lost the rules that had been fixed in workflow.py.
    email = {
        "email_id": email_id,
        "from": payload.sender,
        "subject": payload.subject,
        "body": payload.body,
        "attachments": list(decoded),
    }
    # The bytes came in over HTTP, so resolve attachment names against them
    # rather than letting the evaluator look for files that do not exist.
    verdict = workflow.evaluate_email(email, content_provider=decoded.__getitem__)

    elapsed = (time.perf_counter() - t0) * 1000.0
    return EmailAnalyzeResponse(
        email_id=email_id,
        category=verdict.category,
        confidence=verdict.confidence,
        classification_reason=verdict.classification_reason,
        status=verdict.status,
        has_defect=verdict.has_defect,
        defect_fields=verdict.defect_fields,
        field_results=[FieldResult(**fr) for fr in verdict.field_results],
        review_reason=verdict.review_reason,
        suggested_action=_suggested_action(verdict),
        security_alerts=[],
        processing_ms=elapsed,
    )


def _ingest_response(analysis: EmailAnalyzeResponse, email_id: str,
                     persisted: bool, review_desk_url: str) -> EmailIngestResponse:
    """Project an analyze verdict onto the ingest response shape."""
    return EmailIngestResponse(
        email_id=email_id,
        category=analysis.category,
        confidence=analysis.confidence,
        classification_reason=analysis.classification_reason,
        status=analysis.status,
        has_defect=analysis.has_defect,
        defect_fields=analysis.defect_fields,
        field_results=analysis.field_results,
        review_reason=analysis.review_reason,
        suggested_action=analysis.suggested_action,
        security_alerts=analysis.security_alerts,
        processing_ms=analysis.processing_ms,
        persisted=persisted,
        review_desk_url=review_desk_url,
    )


@router.post("/ingest", response_model=EmailIngestResponse, summary="Stateful email ingestion with Review Desk sync")
def ingest_email(payload: EmailAnalyzeRequest, db: Session = Depends(get_db)):
    """Stateful ingestion endpoint.

    Saves incoming email and attachments to the system, runs the verification pipeline,
    records results in the database, and exposes ambiguous/mismatched emails in the
    Review Desk UI (/ui/).
    """
    email_id = payload.email_id or f"EXT_{uuid.uuid4().hex[:8].upper()}"

    # 1. First run the analyze pipeline
    analysis = analyze_email(payload)

    # A payload the security gate quarantined must not be written to disk.
    # "We refused this file" and "we stored this file" cannot both be true, and
    # the verdict already says the caller should not have sent it.
    if analysis.status == "ERROR":
        log.warning("refusing to persist quarantined payload for %s: %s",
                    email_id, analysis.security_alerts)
        return _ingest_response(analysis, email_id, persisted=False,
                                review_desk_url="")

    # 2. Persist attachments to disk (INGEST_DIR/{email_id}/ by default)
    ingest_dir = Path(get_settings().ingest_dir) / _safe_segment(email_id, "unnamed")
    ingest_dir.mkdir(parents=True, exist_ok=True)

    attachment_paths = []
    for att in payload.attachments:
        content = _decode_attachment(att)
        file_path = ingest_dir / _safe_segment(att.filename, "attachment")
        file_path.write_bytes(content)
        attachment_paths.append(_portable_path(file_path))

    # 3. Upsert EmailRecord
    rec = db.query(EmailRecord).filter_by(email_id=email_id).first()
    if rec is None:
        rec = EmailRecord(email_id=email_id)
        db.add(rec)
    rec.sender = payload.sender
    rec.subject = payload.subject
    rec.body = payload.body
    rec.attachments = attachment_paths
    db.commit()

    # 4. Upsert ReportRecord
    rep = db.query(ReportRecord).filter_by(email_id=email_id).first()
    if rep is None:
        rep = ReportRecord(email_id=email_id)
        db.add(rep)
    rep.category = analysis.category
    rep.status = analysis.status
    rep.has_defect = 1 if analysis.has_defect else 0
    rep.defect_fields = analysis.defect_fields
    rep.review_reason = analysis.review_reason
    rep.field_results = [fr.model_dump() for fr in analysis.field_results]
    rep.processing_ms = analysis.processing_ms
    rep.attempts = (rep.attempts or 0) + 1
    db.commit()

    review_desk_url = f"/ui/?search={email_id}"

    return _ingest_response(analysis, email_id, persisted=True,
                            review_desk_url=review_desk_url)
