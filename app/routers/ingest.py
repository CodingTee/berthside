"""External Ingestion & Verification Router.

Exposes RESTful endpoints for third-party systems (RPA, mail gateways, CRM/ERP)
to send emails into BerthSide for intelligent classification, document extraction,
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
from datetime import datetime
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.config import BACKEND_ROOT, get_settings
from app.database import get_db
from app.models import EmailRecord, ReportRecord, infer_source_mailbox
from app.schemas import (
    AttachmentPayload,
    EmailAnalyzeRequest,
    EmailAnalyzeResponse,
    EmailIngestResponse,
    FieldResult,
)
from app.services import archive, nested_mail, workflow
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


def expand_payloads(
    payloads: list[tuple[str, bytes]],
    budget: Optional[archive.Budget] = None,
) -> tuple[list[tuple[str, bytes]], list[str]]:
    """Expand containers and forwarded messages into comparable documents.

    Returns ``(documents, security_alerts)``. A container is only a carrier: its
    members become the attachments the engine compares, which is why the
    container itself is never kept as a separate document. A forwarded message
    behaves the same way, with one addition: its body becomes a synthetic
    document carrying the ``.frombody`` marker, so it can fill a genuine gap but
    can never displace a real attachment.

    Carriers nest, so this recurses. Every level spends from one shared
    :class:`archive.Budget` and the depth is capped by ``archive.MAX_LEVELS``:
    otherwise "wrap it in a zip" would be a way to multiply the size caps.

    Public because both entry points need it. An attachment that arrives over
    HTTP and one pulled in from Gmail must be expanded the same way, or the same
    zip is readable through one door and unreadable through the other.
    """
    documents: list[tuple[str, bytes]] = []
    security_alerts: list[str] = []
    budget = budget if budget is not None else archive.Budget()
    seen: set[str] = set()

    def absorb(name: str, data: bytes, depth: int) -> None:
        if archive.detect_container(name, data) is not None:
            if depth >= archive.MAX_LEVELS:
                log.info("archive %s: nesting limit reached, not expanded", name)
                return
            expansion = archive.expand_archive(name, data, budget)
            security_alerts.extend(expansion.blocked)
            for member, payload in expansion.members:
                absorb(member, payload, depth + 1)
            if not expansion.members:
                security_alerts.append(
                    f"{name}: no readable shipping documents inside")
            elif expansion.notes:
                log.info("archive %s: %s", name, "; ".join(expansion.notes))
            return

        if nested_mail.is_nested_mail(name, data):
            expansion = nested_mail.expand_mail(name, data, budget)
            security_alerts.extend(expansion.blocked)
            for member, payload in expansion.members:
                absorb(member, payload, depth + 1)
            if expansion.notes:
                log.info("message %s: %s", name, "; ".join(expansion.notes))
            return

        if name in seen:
            # Two carriers produced the same base name. Keeping the first and
            # saying so beats silently replacing a document that was already
            # accepted with an unrelated one of the same name.
            log.info("attachment %s: duplicate name, the first copy is kept", name)
            return
        seen.add(name)
        documents.append((name, data))

    for name, data in payloads:
        absorb(name, data, 0)
    return documents, security_alerts


def _decode_all_attachments(
    attachments: list[AttachmentPayload],
) -> tuple[dict[str, bytes], list[str]]:
    """Decode every attachment, then expand the carriers among them."""
    checked: list[tuple[str, bytes]] = []
    security_alerts: list[str] = []

    for att in attachments:
        # Reject an oversized payload *before* decoding it ...
        if encoded_size_exceeds_cap(att.content_base64, att.content_text):
            security_alerts.append(
                f"{att.filename}: exceeds the "
                f"{MAX_ATTACHMENT_SIZE // (1024 * 1024)}MB attachment limit")
            continue
        content = _decode_attachment(att)
        is_safe, reason = verify_file_safety(att.filename, content)
        if not is_safe:
            security_alerts.append(f"{att.filename}: {reason}")
            continue
        checked.append((att.filename, content))

    documents, alerts = expand_payloads(checked)
    security_alerts.extend(alerts)
    return dict(documents), security_alerts




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


def action_for_verdict(verdict: workflow.EmailVerdict) -> dict | None:
    """Structured workflow action for external integrations / side panels."""
    if verdict.status == "NEEDS_REVIEW" and verdict.review_reason == "missing_attachment":
        missing = verdict.extracted.get("missing_documents") or []
        if missing:
            return {
                "type": "request_document",
                "document": missing[0],
                "missing_documents": missing,
                "label": f"Request {missing[0]}",
            }
    if verdict.status == "MISMATCH":
        return {
            "type": "review_mismatch",
            "fields": verdict.defect_fields,
            "label": "Review mismatch",
        }
    if verdict.status == "NEEDS_REVIEW":
        return {
            "type": "manual_review",
            "reason": verdict.review_reason or "undecidable",
            "label": "Open review",
        }
    return None


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

    Callers that also need the side effects (shipment grouping, versions,
    issues) use `analyze_email_with_verdict` so the verdict is computed **once**
    and handed over, instead of being recomputed on the persistence path.
    """
    response, _verdict = analyze_email_with_verdict(payload)
    return response


def analyze_email_with_verdict(
    payload: EmailAnalyzeRequest,
) -> tuple[EmailAnalyzeResponse, "workflow.EmailVerdict | None"]:
    """`analyze_email`, plus the verdict object it was built from.

    The verdict carries the SI/BL pair — paths, contents and extraction results
    — which is exactly what the persistence path needs. Returning it is what
    lets ``POST /api/process`` create the shipment without extracting anything a
    second time. The verdict is ``None`` for a payload the security gate
    quarantines, because no comparison was run.
    """
    t0 = time.perf_counter()
    email_id = payload.email_id or f"ANL_{uuid.uuid4().hex[:8].upper()}"

    # 1. Decode and verify attachments (ZIP archives expand into members).
    #    Oversized payloads are rejected *before* decoding: the cap exists so a
    #    single request cannot exhaust the process, and a payload rejected only
    #    after a full decode has already spent that memory.
    decoded, security_alerts = _decode_all_attachments(payload.attachments)

    # If any attachment is flagged as dangerous executable/malicious
    if security_alerts:
        elapsed = (time.perf_counter() - t0) * 1000.0
        return EmailAnalyzeResponse(
            email_id=email_id,
            shipment_id=payload.shipment_id,
            category="SPAM",
            confidence=1.0,
            classification_reason="Dangerous or spoofed attachment detected by security gate",
            status="ERROR",
            has_defect=False,
            defect_fields=[],
            field_results=[],
            review_reason="security_quarantine",
            suggested_action="QUARANTINE_AND_ALERT_SECURITY",
            action={
                "type": "quarantine",
                "label": "Quarantine attachment",
                "security_alerts": security_alerts,
            },
            security_alerts=security_alerts,
            processing_ms=elapsed,
        ), None

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
    action = action_for_verdict(verdict)
    missing_documents = verdict.extracted.get("missing_documents") or []
    response = EmailAnalyzeResponse(
        email_id=email_id,
        shipment_id=payload.shipment_id,
        category=verdict.category,
        confidence=verdict.confidence,
        classification_reason=verdict.classification_reason,
        status=verdict.status,
        has_defect=verdict.has_defect,
        defect_fields=verdict.defect_fields,
        possible_fields=verdict.possible_fields,
        field_results=[FieldResult(**fr) for fr in verdict.field_results],
        review_reason=verdict.review_reason,
        suggested_action=_suggested_action(verdict),
        missing_documents=missing_documents,
        action=action,
        security_alerts=[],
        processing_ms=elapsed,
    )
    return response, verdict


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
    # Same decoded set the analysis ran on, so the stored files and the verdict
    # always describe the same documents (archives expanded, unsafe skipped).
    decoded, _alerts = _decode_all_attachments(payload.attachments)
    for name, content in decoded.items():
        file_path = ingest_dir / _safe_segment(name, "attachment")
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
    if not rec.source_mailbox:
        rec.source_mailbox = infer_source_mailbox(email_id, getattr(payload, "sender", None))
    # The caller may know when the mail was actually received; the shipment view
    # shows it as source information, so it is kept when it is supplied.
    received = (payload.metadata or {}).get("received")
    if received:
        try:
            rec.received_at = datetime.fromisoformat(
                str(received).replace("Z", "+00:00"))
        except ValueError:
            log.info("ignoring unparseable received date for %s: %r",
                     email_id, received)
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
