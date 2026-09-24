"""Integration API for the ShipMail UI, the side panel and real Gmail.

Architecture (single source of truth):

    ShipMail (simulated) ─┐
    Real Gmail            ┼─► BerthSide API ─► Existing BerthSide Core ─► Stored Result
    Dashboard        ┘                                                   │
                            Side Panel ◄─────────────────────────────────┘

Nothing in this file classifies, extracts, OCRs or verifies anything. Every
value comes from the existing core (``routers/ingest.analyze_email`` →
``services/workflow.evaluate_email``). This router only

  1. deduplicates requests (email_id / message_id / thread_id / content hash),
  2. stores the result,
  3. returns the stored result on every subsequent call.

That is what keeps Render traffic low and guarantees the side panel and the
Dashboard always show the *same* processing result.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.config import get_settings
from app.database import get_db
from app.models import ProcessResultRecord
from app.routers.ingest import (_decode_all_attachments, _portable_path,
                                _safe_segment, analyze_email_with_verdict)
from app.schemas import (
    ApiHealthOut,
    EmailAnalyzeRequest,
    EmailProcessResponse,
    MockDocumentRequest,
    MockDocumentResponse,
    ProcessDocumentSummary,
)
from app.services import mock_customer_db, result_cache, workflow

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["integration"])


# --------------------------------------------------------------------- health
@router.get("/health", response_model=ApiHealthOut,
            summary="BerthSide API health check")
def api_health(db: Session = Depends(get_db)) -> ApiHealthOut:
    return ApiHealthOut(
        core="existing-berthside-core",
        results_cached=result_cache.count_results(db),
    )


# -------------------------------------------------------------------- process
@router.post("/process", response_model=EmailProcessResponse,
             summary="Process one email through the existing BerthSide core")
def process_email(
    payload: EmailAnalyzeRequest,
    db: Session = Depends(get_db),
) -> EmailProcessResponse:
    """Process an email — once.

    Deduplication is the whole point of this endpoint:

    * the same ``email_id`` / ``message_id`` / ``thread_id`` never produces
      duplicate shipments, document versions, OCR runs, extractions or
      verifications;
    * an unchanged email returns the stored result without touching the
      pipeline at all;
    * only a changed email (new content hash) or an explicit ``force=true``
      (the user pressed "Re-process") runs the core again.
    """
    fingerprint = result_cache.content_fingerprint(
        payload.subject, payload.body, payload.attachments
    )
    email_id, cache_key = result_cache.resolve_identity(
        payload.email_id, payload.message_id, fingerprint
    )
    # Hand the deterministic id to the core so the result is reproducible and
    # the missing-document workflow can find it again later.
    payload.email_id = email_id
    shipment_id = payload.shipment_id

    existing = result_cache.find_cached(
        db,
        cache_key=cache_key,
        email_id=email_id,
        message_id=payload.message_id,
        thread_id=payload.thread_id,
    )

    if existing is not None and not payload.force:
        if existing.content_hash == fingerprint:
            log.info("Reusing stored result for %s (no reprocessing).", cache_key)
            return EmailProcessResponse(
                **result_cache.cached_response(existing, reused=True)
            )
        log.info(
            "Content of %s changed — reprocessing is legitimate.", cache_key
        )

    # ---- the single place where the existing core actually runs -----------
    # The verdict is computed once and handed on: `/api/process` must leave the
    # same shipment / document / version state behind as the corpus path, and
    # re-evaluating would classify, extract, OCR and compare a second time.
    analysis, verdict = analyze_email_with_verdict(payload)
    _persist_shipment_state(db, email_id, payload, verdict)

    doc_fingerprints = _doc_fingerprints(payload)
    versions = result_cache.document_versions(db, shipment_id, doc_fingerprints)

    documents = _document_summaries(
        payload, analysis.missing_documents, versions
    )
    extracted_fields = _flatten_field_values(analysis.field_results)
    verification = {
        "status": analysis.status,
        "has_defect": analysis.has_defect,
        "defect_fields": analysis.defect_fields,
        "possible_fields": analysis.possible_fields,
        "field_results": [fr.model_dump() for fr in analysis.field_results],
        "review_reason": analysis.review_reason,
        "missing_documents": analysis.missing_documents,
    }

    fresh = EmailProcessResponse(
        **analysis.model_dump(),
        documents=documents,
        extracted_fields=extracted_fields,
        verification=verification,
        actions=[analysis.action] if analysis.action else [],
        cached=False,
        cache_key=cache_key,
        attempts=1,
    )

    row = result_cache.store_result(
        db,
        cache_key=cache_key,
        email_id=email_id,
        message_id=payload.message_id,
        thread_id=payload.thread_id,
        shipment_id=shipment_id,
        source=payload.source or result_cache.DEFAULT_SOURCE,
        content_hash=fingerprint,
        doc_fingerprints=doc_fingerprints,
        result=fresh.model_dump(mode="json"),
    )
    return EmailProcessResponse(
        **result_cache.cached_response(row, reused=False)
    )


def _persist_shipment_state(db: Session, email_id: str,
                            payload: EmailAnalyzeRequest, verdict) -> None:
    """Give a `/api/process` run its normal side effects.

    This is the J6 fix. The endpoint used to stop at the verdict, so a shipment
    the ShipMail "Run BerthSide" button had just verified did not exist as far as
    `/shipments` was concerned: the button processed the email and the shipment
    view could not see it.

    Attachments are written to disk before the pipeline runs because the invoice
    and standalone-document paths resolve their content through the stored path,
    exactly as they do when mail arrives via `/api/v1/ingest`.
    """
    if verdict is None:
        # Security-quarantined payload: nothing was compared, nothing to file.
        log.info("no shipment persisted for %s (quarantined payload)", email_id)
        return

    decoded, _alerts = _decode_all_attachments(payload.attachments)
    target_dir = Path(get_settings().ingest_dir) / _safe_segment(email_id, "email")
    target_dir.mkdir(parents=True, exist_ok=True)
    paths: list[str] = []
    for name, content in decoded.items():
        path = target_dir / _safe_segment(name, "attachment")
        path.write_bytes(content)
        paths.append(_portable_path(path))

    email = {
        "email_id": email_id,
        "from": payload.sender,
        "subject": payload.subject,
        "body": payload.body,
        "attachments": paths,
        "received_at": (payload.metadata or {}).get("received"),
    }
    report = workflow.persist_email_with_verdict(db, email, verdict)
    log.info("shipment state persisted for %s (report %s/%s)",
             email_id, report.status, report.category)


# -------------------------------------------------------------------- results
@router.get("/results", response_model=list[EmailProcessResponse],
            summary="Read stored processing results (never reprocesses)")
def list_results(
    limit: int = Query(50, ge=1, le=200),
    db: Session = Depends(get_db),
) -> list[EmailProcessResponse]:
    """Everything the Dashboard and side panel need, straight from storage.

    Opening or refreshing the Dashboard hits this — never the pipeline.
    """
    return [
        EmailProcessResponse(**item)
        for item in result_cache.list_results(db, limit=limit)
    ]


@router.get("/results/{key}", response_model=EmailProcessResponse,
            summary="Read one stored processing result (never reprocesses)")
def get_result(key: str, db: Session = Depends(get_db)) -> EmailProcessResponse:
    """Fetch a stored result by email_id, message_id or cache_key.

    Returns 404 when the email has not been processed yet; the caller then
    decides whether to run ``POST /api/process``. Reading never triggers
    processing on its own.
    """
    row = (
        db.query(ProcessResultRecord)
        .filter(
            (ProcessResultRecord.cache_key == key)
            | (ProcessResultRecord.email_id == key)
            | (ProcessResultRecord.message_id == key)
        )
        .order_by(ProcessResultRecord.id.desc())
        .first()
    )
    if row is None:
        raise HTTPException(404, f"no stored result for: {key}")
    return EmailProcessResponse(
        **result_cache.cached_response(row, reused=True)
    )


# ------------------------------------------------------- mock customer DB
@router.post("/mock-customer-db/request-document",
             response_model=MockDocumentResponse,
             summary="Retrieve a missing document from the mock customer DB")
def request_mock_document(payload: MockDocumentRequest) -> MockDocumentResponse:
    attachment = mock_customer_db.request_document(
        payload.shipment_id, payload.document_type
    )
    if attachment is None:
        return MockDocumentResponse(
            found=False,
            shipment_id=payload.shipment_id,
            document_type=payload.document_type,
            message="Document not found in mock customer database.",
        )
    return MockDocumentResponse(
        found=True,
        shipment_id=payload.shipment_id,
        document_type=payload.document_type,
        attachment=attachment,
        message="Document retrieved from mock customer database.",
    )


# ------------------------------------------------------------------ helpers
def _doc_fingerprints(payload: EmailAnalyzeRequest) -> dict[str, str]:
    """{doc_type: attachment hash} — the basis for SI/BL version numbers."""
    out: dict[str, str] = {}
    for att in payload.attachments:
        doc_type = _doc_type_from_name(att.filename)
        if not doc_type:
            continue
        fp = result_cache.attachment_fingerprint(att)
        # Keep the newest revision's fingerprint for this document type.
        out[doc_type] = fp
    return out


def _document_summaries(
    payload: EmailAnalyzeRequest,
    missing_documents: list[str],
    versions: dict[str, int] | None = None,
) -> list[ProcessDocumentSummary]:
    versions = versions or {}
    out: list[ProcessDocumentSummary] = []
    for att in payload.attachments:
        doc_type = _doc_type_from_name(att.filename)
        version = versions.get(doc_type) if doc_type else None
        out.append(ProcessDocumentSummary(
            type=doc_type or "UNKNOWN",
            filename=att.filename,
            status="detected" if doc_type else "received",
            version=f"v{version}" if version else None,
        ))
    for missing in missing_documents:
        out.append(ProcessDocumentSummary(
            type=missing,
            filename=None,
            status="missing",
        ))
    return out


# `SI` / `BL` as a standalone token, so "SI_SHP-002_v1.txt" and
# "Draft_BL_SHP-002.txt" are recognised while "SILVER_LINING.pdf" is not.
_SI_TOKEN = re.compile(r"(?:^|[^a-z])si(?:[^a-z]|$)")
_BL_TOKEN = re.compile(r"(?:^|[^a-z])bl(?:[^a-z]|$)")


def _doc_type_from_name(filename: str) -> str | None:
    lower = (filename or "").lower()
    compact = "".join(ch for ch in lower if ch.isalnum())
    if "shippinginstruction" in compact or _SI_TOKEN.search(lower):
        return "SI"
    if "billoflading" in compact or "draftbl" in compact or _BL_TOKEN.search(lower):
        return "BL"
    if "invoice" in lower or "_inv" in lower or lower.startswith("inv"):
        return "INVOICE"
    return None


def _flatten_field_values(field_results) -> dict:
    values = {}
    for fr in field_results:
        values[fr.field] = {
            "si": fr.si_value,
            "bl": fr.bl_value,
            "match": fr.match,
        }
    return values
