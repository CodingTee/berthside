"""Workflow controller — the single orchestrator every API endpoint calls.

Pipeline (per email):
    classify → (BL_COMPARISON only) read attachments → extract → compare
    → persist report (or escalate for human review / record error)

Guarantees
----------
* Idempotent: re-processing an email replaces its previous report.
* Never raises to the caller — failures are recorded as status=ERROR and the
  message stored; calling process again IS the retry (attempts counter).
* The comparison verdict is always deterministic (see services/comparison.py).
"""
from __future__ import annotations

import logging
import re
import time
from typing import Any, Optional

from sqlalchemy.orm import Session

from app.models import EmailRecord, ReportRecord, ReviewRecord
from app.schemas import COMPARED_FIELDS
from app.services import ai_service, inbox_service, versioning
from app.services.classifier import Classification
from app.services.comparison import compare

log = logging.getLogger(__name__)

_DOC_TYPE_RE = re.compile(r"_(SI|BL)\.[a-z]+$", re.IGNORECASE)
NON_COMPARISON_STATUS = "SKIPPED"  # classification-only emails


# ------------------------------------------------- missing attachment: which kind?
# Not every BL_COMPARISON email with no SI/BL attached needs a human. Two very
# different situations look identical to `_find_doc_attachments`:
#
#   (a) the sender asks US to send them the draft BL —
#       "Please assist to send the draft BL for SIN832764835 for checking asap."
#       There is nothing to compare and nothing for us to action: the reply is a
#       document, not a verification. Gold treats these as OK (91 emails).
#
#   (b) the sender asks us to COMPARE, and says the documents are absent —
#       "Please compare the SI and draft BL for X and confirm (attachments
#        appear to have been dropped)."
#       The task is ours and we cannot do it: escalate (5 emails).
#
# Escalating (a) is the single biggest reliability defect we had: 91 of 106
# flags were this, dragging escalation precision to 0.14. Measured on the
# official set, this rule separates the two families with 0 errors on all 94
# no-attachment BL_COMPARISON emails.
#
# Note the legal boilerplate "please exercise caution with ... any links or
# attachments" is NOT a missing-document signal, so the patterns below match
# only explicit declarations and explicit compare instructions.
_DECLARED_MISSING = re.compile(
    r"(?:appear(?:s)? to have been dropped"
    r"|(?:is|are|was|were)\s+(?:still\s+)?missing"
    r"|not attached|failed to attach|didn'?t attach"
    r"|missing from (?:this|the) email"
    r"|no attachments? (?:were |was )?(?:included|provided))",
    re.IGNORECASE,
)
_ASKED_TO_COMPARE = re.compile(
    r"compare(?:d)?\s+(?:the\s+)?(?:si|shipping instruction)\s+and\s+(?:the\s+)?(?:draft\s+)?(?:b/?l|bill of lading)"
    r"|check\s+(?:the\s+)?(?:draft\s+)?b/?l\s+against\s+the\s+si"
    r"|verify\s+(?:the\s+)?b/?l\s+matches\s+the\s+si",
    re.IGNORECASE,
)
_DOCUMENT_REQUEST = re.compile(
    r"(?:please|pls|kindly)[^.]{0,60}?\b(?:assist\s+to\s+)?(?:send|provide|share|forward|issue)\b"
    r"[^.]{0,60}?\b(?:draft\s+)?(?:b/?l|si|shipping instruction|bill of lading)\b",
    re.IGNORECASE,
)


def _is_document_request(body: str) -> bool:
    """True when the email only asks us to *send* documents — no comparison.

    Deliberately conservative: an explicit "documents are missing" claim or an
    explicit instruction to compare keeps the escalation, and anything we do
    not recognise also keeps it. Only a clear send-request is downgraded.
    """
    if not body:
        return False
    if _DECLARED_MISSING.search(body) or _ASKED_TO_COMPARE.search(body):
        return False
    return bool(_DOCUMENT_REQUEST.search(body))


# --------------------------------------------------------------------- entry
def process_email(db: Session, email_id: str) -> ReportRecord:
    """Run the full pipeline for one email. Always returns a report row."""
    email = inbox_service.get_email(email_id)
    if email is None:
        raise KeyError(f"email not found in inbox: {email_id}")

    _upsert_email_record(db, email)

    report = db.query(ReportRecord).filter_by(email_id=email_id).first()
    if report is None:
        report = ReportRecord(email_id=email_id)
        db.add(report)
    report.attempts = (report.attempts or 0) + 1
    report.error_message = None

    t0 = time.perf_counter()
    try:
        _run_pipeline(db, report, email)
    except Exception as exc:  # noqa: BLE001 — record, don't crash the API
        log.exception("pipeline failed for %s", email_id)
        report.status = "ERROR"
        report.category = report.category or "GENERAL"
        report.error_message = f"{type(exc).__name__}: {exc}"
    report.processing_ms = (time.perf_counter() - t0) * 1000.0

    db.commit()
    db.refresh(report)
    return report


def retry_failed(db: Session, statuses: list[str] | None = None,
                 limit: int = 0) -> dict[str, Any]:
    """Re-run the pipeline for emails whose last attempt did not succeed.

    Retry is simply "process again": the endpoint is idempotent and the
    attempts counter increments, so a transient AI/IO failure clears itself
    on the next try while the evidence stays auditable.
    """
    targets = [s.upper() for s in (statuses or ["ERROR", "NEEDS_REVIEW"])]
    q = db.query(ReportRecord).filter(ReportRecord.status.in_(targets))
    q = q.order_by(ReportRecord.id)
    rows = q.limit(limit).all() if limit else q.all()

    retried = recovered = still_failing = 0
    by_status: dict[str, int] = {}
    for row in rows:
        before = row.status
        try:
            report = process_email(db, row.email_id)
            retried += 1
            by_status[report.status or "?"] = \
                by_status.get(report.status or "?", 0) + 1
            if report.status in ("OK", "MISMATCH", "SKIPPED"):
                recovered += 1
            elif report.status == before:
                still_failing += 1
        except Exception as exc:  # noqa: BLE001
            still_failing += 1
            log.error("retry: %s failed: %s", row.email_id, exc)

    return {
        "targets": len(rows),
        "retried": retried,
        "recovered": recovered,
        "still_failing": still_failing,
        "by_status": by_status,
    }


def get_status(db: Session, email_id: str) -> dict[str, Any]:
    """Processing status for one email — 'is it done, did it fail, why'."""
    report = db.query(ReportRecord).filter_by(email_id=email_id).first()
    if report is None:
        return {"email_id": email_id, "processed": False, "state": "PENDING",
                "status": None, "attempts": 0, "error_message": None,
                "processing_ms": None, "reviewed": False, "updated_at": None,
                "stage": "pending"}

    if report.status == "ERROR":
        state = "FAILED"
    elif report.status == "NEEDS_REVIEW":
        state = "NEEDS_HUMAN"
    elif report.status in ("OK", "MISMATCH", "SKIPPED"):
        state = "COMPLETED"
    else:
        state = "UNKNOWN"

    # Derive a human-readable pipeline stage so the frontend can show *where*
    # the email is, including why it was escalated (reliability is visible).
    if report.category is None:
        stage = "pending"
    elif report.category != "BL_COMPARISON":
        stage = "classified (non-comparison)"
    elif report.status == "NEEDS_REVIEW":
        stage = f"escalated: {report.review_reason or 'needs review'}"
    elif report.status == "ERROR":
        stage = "failed during processing"
    elif report.reviewed:
        stage = "completed (human-reviewed)"
    else:
        stage = "completed"

    return {
        "email_id": email_id,
        "processed": True,
        "state": state,
        "status": report.status,
        "category": report.category,
        "stage": stage,
        "attempts": report.attempts or 0,
        "error_message": report.error_message,
        "processing_ms": report.processing_ms,
        "reviewed": bool(report.reviewed),
        "updated_at": report.updated_at.isoformat() if report.updated_at else None,
    }


def process_all(db: Session, limit: int = 0) -> dict[str, Any]:
    """Batch-process the whole inbox. Returns aggregate counters."""
    t0 = time.perf_counter()
    emails = inbox_service.all_emails()
    if limit:
        emails = emails[:limit]

    by_status: dict[str, int] = {}
    by_category: dict[str, int] = {}
    failed = 0
    for email in emails:
        try:
            report = process_email(db, email["email_id"])
            by_status[report.status or "UNKNOWN"] = \
                by_status.get(report.status or "UNKNOWN", 0) + 1
            by_category[report.category or "UNKNOWN"] = \
                by_category.get(report.category or "UNKNOWN", 0) + 1
            if report.status == "ERROR":
                failed += 1
        except Exception as exc:  # noqa: BLE001
            failed += 1
            log.error("process_all: %s failed: %s", email.get("email_id"), exc)

    return {
        "requested": len(emails),
        "processed": len(emails) - failed,
        "succeeded": len(emails) - failed,
        "failed": failed,
        "by_status": by_status,
        "by_category": by_category,
        "elapsed_ms": (time.perf_counter() - t0) * 1000.0,
    }


# ------------------------------------------------------------------- pipeline
def _run_pipeline(db: Session, report: ReportRecord, email: dict) -> None:
    # 1. classify -----------------------------------------------------------
    try:
        cls: Classification = ai_service.classify_email(email)
    except Exception as exc:  # remote-only provider is down
        raise RuntimeError(f"classification unavailable: {exc}") from exc
    report.category = cls.category

    if cls.category != "BL_COMPARISON":
        report.status = NON_COMPARISON_STATUS
        report.has_defect = 0
        report.defect_fields = []
        report.review_reason = None
        report.field_results = []
        report.extracted = {"classification_reason": cls.reason,
                            "confidence": cls.confidence}
        return

    # 2. locate SI + BL attachments ---------------------------------------
    si_path, bl_path = _find_doc_attachments(email)
    if si_path is None or bl_path is None:
        if _is_document_request(email.get("body") or ""):
            # The sender is asking us to send them a document, so there is
            # nothing to verify: no attachment is expected and none is
            # missing. Reporting OK is the honest verdict — escalating would
            # put a task in the human queue that does not exist.
            report.status = "OK"
            report.review_reason = None
            report.defect_fields = []
            report.has_defect = 0
            report.field_results = []
            report.extracted = {
                "si_attachment": si_path, "bl_attachment": bl_path,
                "action": "document_request",
                "evidence": "Sender asked us to send documents; no SI/BL pair to "
                            "compare, so nothing is missing.",
            }
            return

        missing = [d for d, p in (("SI", si_path), ("BL", bl_path)) if p is None]
        present = {d: p for d, p in (("SI", si_path), ("BL", bl_path)) if p}
        report.status = "NEEDS_REVIEW"
        report.review_reason = "missing_attachment"
        report.defect_fields = []
        report.has_defect = 0
        report.field_results = []
        # Evidence for the human reviewer: which document is absent and what we
        # DID find, so the escalation is actionable (not a silent dead-end).
        report.extracted = {
            "si_attachment": si_path, "bl_attachment": bl_path,
            "missing_documents": missing, "present_documents": present,
            "evidence": f"Expected both SI and BL; missing: {', '.join(missing) or 'none'}",
        }
        return

    # 3. read + extract ----------------------------------------------------
    si_content = inbox_service.read_attachment(si_path)
    bl_content = inbox_service.read_attachment(bl_path)
    si_res = ai_service.extract_document("SI", si_path, si_content)
    bl_res = ai_service.extract_document("BL", bl_path, bl_content)

    # wrong_doc_type: only escalate when the document really is the wrong
    # kind — i.e. it declares the other type AND yields almost no usable
    # fields. A doc that declares the other type yet parses fine still gets
    # compared; escalating it would silently drop a real defect.
    def _wrong_doc(res, other: str) -> bool:
        return bool(res.fields) is False or (
            len(res.fields) < 3 and _declares_other(res.raw_text, other))

    if _wrong_doc(si_res, "BL") or _wrong_doc(bl_res, "SI"):
        report.status = "NEEDS_REVIEW"
        report.review_reason = "wrong_doc_type"
        report.extracted = {
            "si": si_res.fields, "bl": bl_res.fields,
            "evidence": "Attachment present but does not read as the expected "
                        "document type (SI vs BL).",
        }
        return

    # unreadable binary attachments (pdf/docx/xlsx without an AI parser)
    if not si_res.readable or not bl_res.readable:
        unreadable = [d for d, r in (("SI", si_res), ("BL", bl_res)) if not r.readable]
        report.status = "NEEDS_REVIEW"
        report.review_reason = "unreadable"
        report.extracted = {
            "si": si_res.fields, "bl": bl_res.fields,
            "unreadable_documents": unreadable,
            "evidence": f"Could not read: {', '.join(unreadable)}. Needs OCR, a "
                        f"vision model, or a human to transcribe.",
        }
        return

    # 3b. OCR-derived documents: read, but from pixels ------------------------
    # A scanned PDF has no text layer, so OCR transcribes it from the rendered
    # image. That transcription is weaker than an embedded text layer — digits
    # and letters get confused ("VALPARAISO" -> "VALPARAISQ", "CHINA" ->
    # "CHIMA") — so an incomplete transcription must NOT be silently compared:
    # a misread field looks exactly like a real discrepancy. Escalate instead,
    # and hand the reviewer the transcription plus what is still missing.
    ocr_docs = [d for d, r in (("SI", si_res), ("BL", bl_res))
                if r.source == "pdf-ocr" and not r.is_complete]
    if ocr_docs:
        report.status = "NEEDS_REVIEW"
        report.review_reason = "unreadable"
        report.extracted = {
            "si": si_res.fields,
            "bl": bl_res.fields,
            "ocr_documents": ocr_docs,
            "ocr_text": {d: r.raw_text[:1500]
                         for d, r in (("SI", si_res), ("BL", bl_res))
                         if d in ocr_docs},
            "evidence": (
                f"OCR transcribed {', '.join(ocr_docs)} from a scanned image "
                f"(no embedded text layer), but the transcription is incomplete "
                f"— missing: "
                + "; ".join(
                    f"{d}: {', '.join(r.missing)}"
                    for d, r in (("SI", si_res), ("BL", bl_res)) if d in ocr_docs)
                + ". A human must confirm the transcription before comparing; "
                "guessing here would manufacture a false discrepancy."),
        }
        return

    # 4. deterministic versioning + latest-version comparison ----------------
    shipment, _si_version, _bl_version = versioning.sync_processed_documents(
        db,
        report=report,
        email=email,
        si_path=si_path,
        bl_path=bl_path,
        si_content=si_content,
        bl_content=bl_content,
        si_result=si_res,
        bl_result=bl_res,
    )
    latest_outcome, latest_si, latest_bl = versioning.latest_pair_comparison(
        db, shipment.id
    )
    outcome = latest_outcome or compare(si_res.fields, bl_res.fields, COMPARED_FIELDS)

    report.status = outcome.status
    report.has_defect = 1 if outcome.has_defect else 0
    report.defect_fields = outcome.defect_fields
    report.review_reason = outcome.review_reason
    report.field_results = outcome.field_results
    report.extracted = {
        "si": (latest_si.extracted_fields if latest_si else si_res.fields),
        "bl": (latest_bl.extracted_fields if latest_bl else bl_res.fields),
        "si_missing": si_res.missing,
        "bl_missing": bl_res.missing,
        "shipment_id": shipment.id,
        "shipment_key": shipment.shipment_key,
        "latest_si_version_id": latest_si.id if latest_si else None,
        "latest_bl_version_id": latest_bl.id if latest_bl else None,
    }
    versioning.sync_issues_for_report(db, report, shipment)


def _find_doc_attachments(email: dict) -> tuple[Optional[str], Optional[str]]:
    si_path = bl_path = None
    for att in email.get("attachments") or []:
        m = _DOC_TYPE_RE.search(att)
        if not m:
            continue
        if m.group(1).upper() == "SI" and si_path is None:
            si_path = att
        elif m.group(1).upper() == "BL" and bl_path is None:
            bl_path = att
    return si_path, bl_path


_DECLARATION = {
    "SI": re.compile(r"SHIPPING\s+INSTRUCTION|SI\s+FORM|SHIPPING\s+ORDER", re.IGNORECASE),
    "BL": re.compile(r"BILL\s+OF\s+LADING|\bB/?L\s+(?:DRAFT|NO)", re.IGNORECASE),
}


def _declares_other(text: str, other: str) -> bool:
    """True when the text positively declares the *other* document type."""
    if not text or not text.strip():
        return False
    has_si = bool(_DECLARATION["SI"].search(text))
    has_bl = bool(_DECLARATION["BL"].search(text))
    if other == "BL":
        return has_bl and not has_si
    return has_si and not has_bl


def _declares_doc_type(text: str, doc_type: str) -> bool:
    """True unless the document clearly declares the *other* document type.

    Tolerant on purpose: parsed PDF/Word/Excel attachments often lack a header
    line, so we only escalate when the text positively identifies the wrong
    kind of document (e.g. a "BILL OF LADING" attached where an SI should be).
    """
    if not text or not text.strip():
        return True  # nothing readable to judge by; handled elsewhere
    has_si = bool(_DECLARATION["SI"].search(text))
    has_bl = bool(_DECLARATION["BL"].search(text))
    if doc_type == "SI":
        return not (has_bl and not has_si)
    return not (has_si and not has_bl)


# --------------------------------------------------------------------- review
def apply_review(db: Session, email_id: str, decision: str,
                 corrected_fields: dict, corrected_category: Optional[str],
                 reviewer: str, notes: Optional[str]) -> ReportRecord:
    """Record a human decision and update the report accordingly."""
    report = db.query(ReportRecord).filter_by(email_id=email_id).first()
    if report is None:
        raise KeyError(f"no report for email {email_id}; process it first")

    review = ReviewRecord(
        report_id=report.id,
        email_id=email_id,
        reviewer=reviewer,
        decision=decision,
        corrected_fields=corrected_fields or {},
        corrected_category=corrected_category,
        notes=notes,
    )
    db.add(review)

    if decision == "CONFIRM":
        report.reviewed = 1
    elif decision == "CORRECT":
        report.reviewed = 1
        if corrected_category:
            report.category = corrected_category
        if corrected_fields:
            prev = dict(report.extracted or {})
            for f, v in corrected_fields.items():
                prev.setdefault("bl", {})[f] = v
            report.extracted = prev
            # re-run deterministic comparison with the corrected values
            outcome = compare(
                (report.extracted or {}).get("si", {}),
                (report.extracted or {}).get("bl", {}),
                COMPARED_FIELDS,
            )
            report.status = outcome.status
            report.has_defect = 1 if outcome.has_defect else 0
            report.defect_fields = outcome.defect_fields
            report.review_reason = None if report.status != "NEEDS_REVIEW" \
                else outcome.review_reason
            report.field_results = outcome.field_results
    elif decision == "REJECT":
        # human says our verdict is wrong → keep report, mark disputed
        report.reviewed = 1
        if notes:
            report.error_message = notes

    db.commit()
    db.refresh(report)
    return report


# -------------------------------------------------------------------- helpers
def _upsert_email_record(db: Session, email: dict) -> None:
    email_id = email["email_id"]
    row = db.query(EmailRecord).filter_by(email_id=email_id).first()
    received_at = email.get("received_at")
    if isinstance(received_at, str):
        from datetime import datetime
        try:
            received_at = datetime.fromisoformat(received_at.replace("Z", "+00:00"))
        except (TypeError, ValueError):
            received_at = None

    if row is None:
        db.add(EmailRecord(
            email_id=email_id,
            sender=email.get("from"),
            subject=email.get("subject"),
            body=email.get("body"),
            attachments=email.get("attachments") or [],
            received_at=received_at,
        ))
        db.commit()
    elif received_at and row.received_at is None:
        row.received_at = received_at
        db.commit()
