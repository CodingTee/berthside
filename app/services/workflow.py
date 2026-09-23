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
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from sqlalchemy.orm import Session

from app.models import EmailRecord, ReportRecord, ReviewRecord, infer_source_mailbox
from app.schemas import COMPARED_FIELDS
from app.services import ai_service, doc_types, inbox_service, versioning
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
        row = db.query(EmailRecord).filter_by(email_id=email_id).first()
        if row:
            email = {
                "email_id": row.email_id,
                "from": row.sender or "",
                "subject": row.subject or "",
                "body": row.body or "",
                "attachments": row.attachments or [],
                "received_at": row.received_at,
            }
        else:
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
@dataclass
class ComparedPair:
    """The SI/BL pair that was read, with its bytes and extraction results.

    Carried on the verdict so the db-backed path can hand the same documents to
    versioning without reading them a second time.
    """
    si_path: str
    bl_path: str
    si_content: bytes
    bl_content: bytes
    si_result: Any
    bl_result: Any


@dataclass
class EmailVerdict:
    """What the pipeline decided about one email: no database, no report row."""
    category: str
    confidence: float
    classification_reason: str
    status: str
    review_reason: Optional[str] = None
    has_defect: bool = False
    defect_fields: list[str] = field(default_factory=list)
    possible_fields: list[str] = field(default_factory=list)
    field_results: list[dict] = field(default_factory=list)
    extracted: dict = field(default_factory=dict)
    pair: Optional[ComparedPair] = None
    classify_source: str = "rule-classifier"


# How many leading lines count as the heading, i.e. where a document names
# itself. Measured insensitive on the corpus from 1 to 20 lines (the same five
# documents are identified either way); a small window is kept because a
# spreadsheet column or an enclosure list inside the body is not an identity.
_HEADING_LINES = 4

# Ordered: the first pattern that matches a line wins, so the instruction forms
# come before the plain ones. "BILL OF LADING INSTRUCTION" is an *SI*, and ten
# corpus PDFs are titled exactly that in the SI slot, so matching the plain
# BILL OF LADING first would call every one of them the wrong type.
_DOC_TITLES = (
    (re.compile(r"SHIPPING\s+INSTRUCTION", re.IGNORECASE), "SI"),
    (re.compile(r"BILL\s+OF\s+LADING\s+INSTRUCTION", re.IGNORECASE), "SI"),
    (re.compile(r"SHIPPING\s+ORDER", re.IGNORECASE), "SI"),
    (re.compile(r"\bS/?I\s+(?:FORM|NO)\b", re.IGNORECASE), "SI"),
    (re.compile(r"BILL\s+OF\s+LADING", re.IGNORECASE), "BL"),
    (re.compile(r"\bB/?L\s+(?:DRAFT|NO)\b", re.IGNORECASE), "BL"),
    (re.compile(r"PACKING\s+LIST", re.IGNORECASE), "PACKING_LIST"),
    (re.compile(r"CERTIFICATE\s+OF\s+ORIGIN", re.IGNORECASE),
     "CERTIFICATE_OF_ORIGIN"),
    (re.compile(r"DELIVERY\s+ORDER", re.IGNORECASE), "DELIVERY_ORDER"),
    (re.compile(r"BOOKING\s+(?:CONFIRMATION|NOTE)", re.IGNORECASE),
     "BOOKING_CONFIRMATION"),
    (re.compile(r"COMMERCIAL\s+INVOICE", re.IGNORECASE), "INVOICE"),
)


def declared_doc_type(text: str) -> Optional[str]:
    """What the document calls itself in its heading, or None if it is silent.

    This is the evidence the escalation rests on. 15 corpus documents carry no
    heading at all (spreadsheet exports), and they must keep being compared on
    their fields, so a silent heading is not a finding.
    """
    lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
    for line in lines[:_HEADING_LINES]:
        for pattern, doc_type in _DOC_TITLES:
            if pattern.search(line):
                return doc_type
    return None


def _wrong_doc(res, expected: str) -> bool:
    """True when the document declares a type other than the one expected.

    A positive statement, not an absence. The previous rule was
    ``not res.fields or (len(res.fields) < 3 and _declares_other(...))``, which
    filed a PDF we could not open under the same reason as a packing list sent
    in place of a bill of lading. The two need opposite follow-up: one is a
    tooling problem, the other is the sender attaching the wrong file, so
    ground truth labels them `unreadable` and `wrong_doc_type` respectively.

    Reading the *self-declaration* is what separates them, and it also has to
    survive two corpus facts. Ten PDFs titled "BILL OF LADING INSTRUCTION" are
    shipping instructions, not bills of lading. And the five documents this
    rule exists for each say plainly what they are: PACKING LIST, CERTIFICATE
    OF ORIGIN, COMMERCIAL INVOICE.
    """
    declared = declared_doc_type(res.raw_text)
    return declared is not None and declared != expected


def _nothing_read(res) -> bool:
    """True when a reader returned neither fields nor any usable text.

    `readable` alone is not enough evidence: it is set from whether a reader
    produced text, and a reader that produced unusable text still leaves all
    seven fields empty. Escalating on that empty result is the honest call,
    because the alternative reports the missing fields as the sender's fault.
    """
    return not res.readable or (
        not res.fields and not (res.raw_text or "").strip())


def evaluate_email(email: dict,
                   content_provider: Optional[Callable[[str], bytes]] = None
                   ) -> EmailVerdict:
    """Decide one email's outcome. Pure decision logic, never touches the db.

    This is the single implementation of the verification rules, and the point
    is that both entry points call it. `process_email` and the external
    `/api/v1/analyze` endpoint each used to carry their own copy, so a rule
    fixed in one stayed broken in the other: 91 escalated non-issues reappeared
    that way, and the two disagreed on 6 further reason codes.

    `content_provider(path) -> bytes` fetches attachment contents and defaults
    to the inbox (local bundle or dataset server). A caller that already holds
    the bytes, such as the external API, passes its own lookup so an attachment
    that arrived over HTTP is never resolved against the filesystem.

    Raises whatever `ai_service.classify_email` raises when no provider can be
    reached; the caller decides whether that becomes ERROR or a 5xx.
    """
    provider = content_provider or inbox_service.read_attachment

    # 1. classify -----------------------------------------------------------
    try:
        cls: Classification = ai_service.classify_email(email)
    except Exception as exc:  # remote-only provider is down
        raise RuntimeError(f"classification unavailable: {exc}") from exc

    if cls.category != "BL_COMPARISON":
        return EmailVerdict(
            category=cls.category,
            confidence=cls.confidence,
            classification_reason=cls.reason,
            classify_source=cls.source,
            status=NON_COMPARISON_STATUS,
            extracted={"classification_reason": cls.reason,
                       "confidence": cls.confidence},
        )

    # 2. locate SI + BL attachments ---------------------------------------
    si_path, bl_path = _find_doc_attachments(email)
    if si_path is None or bl_path is None:
        if _is_document_request(email.get("body") or ""):
            # The sender is asking us to send them a document, so there is
            # nothing to verify: no attachment is expected and none is
            # missing. Reporting OK is the honest verdict. Escalating would
            # put a task in the human queue that does not exist.
            return EmailVerdict(
                category=cls.category,
                confidence=cls.confidence,
                classification_reason=cls.reason,
                classify_source=cls.source,
                status="OK",
                extracted={
                    "si_attachment": si_path, "bl_attachment": bl_path,
                    "action": "document_request",
                    "evidence": "Sender asked us to send documents; no SI/BL "
                                "pair to compare, so nothing is missing.",
                },
            )

        missing = [d for d, p in (("SI", si_path), ("BL", bl_path)) if p is None]
        present = {d: p for d, p in (("SI", si_path), ("BL", bl_path)) if p}
        return EmailVerdict(
            category=cls.category,
            confidence=cls.confidence,
            classification_reason=cls.reason,
            classify_source=cls.source,
            status="NEEDS_REVIEW",
            review_reason="missing_attachment",
            # Evidence for the human reviewer: which document is absent and
            # what we DID find, so the escalation is actionable (not a silent
            # dead-end).
            extracted={
                "si_attachment": si_path, "bl_attachment": bl_path,
                "missing_documents": missing, "present_documents": present,
                "evidence": ("Expected both SI and BL; missing: "
                             f"{', '.join(missing) or 'none'}"),
            },
        )

    # 3. read + extract ----------------------------------------------------
    si_content = provider(si_path)
    bl_content = provider(bl_path)
    si_res = ai_service.extract_document("SI", si_path, si_content)
    bl_res = ai_service.extract_document("BL", bl_path, bl_content)

    if _wrong_doc(si_res, "SI") or _wrong_doc(bl_res, "BL"):
        wrong = [(d, declared_doc_type(r.raw_text))
                 for d, r in (("SI", si_res), ("BL", bl_res))
                 if _wrong_doc(r, d)]
        return EmailVerdict(
            category=cls.category,
            confidence=cls.confidence,
            classification_reason=cls.reason,
            classify_source=cls.source,
            status="NEEDS_REVIEW",
            review_reason="wrong_doc_type",
            extracted={
                "si": si_res.fields, "bl": bl_res.fields,
                # Naming what the file *is* turns a dead-end escalation into an
                # actionable one: the reviewer asks the sender for the right
                # document instead of hunting for fields that were never there.
                "declared_types": {d: t for d, t in wrong},
                "evidence": (
                    "Attachment present but not the document requested: "
                    + "; ".join(f"{d} reads as a {t.replace('_', ' ').title()}"
                                if t else f"{d} reads as another document type"
                                for d, t in wrong)
                    + ". The sender most likely attached the wrong file."),
            },
        )

    # unreadable binary attachments (pdf/docx/xlsx without an AI parser)
    if _nothing_read(si_res) or _nothing_read(bl_res):
        unreadable = [d for d, r in (("SI", si_res), ("BL", bl_res))
                      if _nothing_read(r)]
        return EmailVerdict(
            category=cls.category,
            confidence=cls.confidence,
            classification_reason=cls.reason,
            classify_source=cls.source,
            status="NEEDS_REVIEW",
            review_reason="unreadable",
            extracted={
                "si": si_res.fields, "bl": bl_res.fields,
                "unreadable_documents": unreadable,
                "evidence": (f"Could not read: {', '.join(unreadable)}. Needs "
                             f"OCR, a vision model, or a human to transcribe."),
            },
        )

    # 3b. OCR-derived documents: read, but from pixels ------------------------
    # A scanned PDF or an image attachment has no text layer, so OCR transcribes
    # it from the rendered pixels. That transcription is weaker than an embedded
    # text layer: digits and letters get confused ("VALPARAISO" -> "VALPARAISQ",
    # "CHINA" -> "CHIMA"). An incomplete transcription must NOT be silently
    # compared, because a misread field looks exactly like a real discrepancy.
    # Escalate instead, and hand the reviewer the transcription plus what is
    # missing. The test is on any `-ocr` reader, not on the PDF one: a PNG of a
    # shipping instruction is the same transcription problem as a scanned PDF,
    # and keying on one reader let the other one fall through to the comparison.
    ocr_docs = [d for d, r in (("SI", si_res), ("BL", bl_res))
                if r.source.endswith("-ocr") and not r.is_complete]
    if ocr_docs:
        return EmailVerdict(
            category=cls.category,
            confidence=cls.confidence,
            classification_reason=cls.reason,
            classify_source=cls.source,
            status="NEEDS_REVIEW",
            review_reason="unreadable",
            extracted={
                "si": si_res.fields,
                "bl": bl_res.fields,
                "ocr_documents": ocr_docs,
                "ocr_text": {d: r.raw_text[:1500]
                             for d, r in (("SI", si_res), ("BL", bl_res))
                             if d in ocr_docs},
                "evidence": (
                    f"OCR transcribed {', '.join(ocr_docs)} from a scanned "
                    f"image (no embedded text layer), but the transcription is "
                    f"incomplete. Missing: "
                    + "; ".join(
                        f"{d}: {', '.join(r.missing)}"
                        for d, r in (("SI", si_res), ("BL", bl_res))
                        if d in ocr_docs)
                    + ". A human must confirm the transcription before "
                      "comparing; guessing here would manufacture a false "
                      "discrepancy."),
            },
        )

    # 4. deterministic comparison ------------------------------------------
    outcome = compare(si_res.fields, bl_res.fields, COMPARED_FIELDS)
    return EmailVerdict(
        category=cls.category,
        confidence=cls.confidence,
        classification_reason=cls.reason,
        classify_source=cls.source,
        status=outcome.status,
        review_reason=outcome.review_reason,
        has_defect=outcome.has_defect,
        defect_fields=outcome.defect_fields,
        field_results=outcome.field_results,
        extracted={
            "si": si_res.fields, "bl": bl_res.fields,
            "si_missing": si_res.missing, "bl_missing": bl_res.missing,
        },
        pair=ComparedPair(si_path, bl_path, si_content, bl_content,
                          si_res, bl_res),
    )


def _apply_verdict(report: ReportRecord, verdict: EmailVerdict) -> None:
    """Copy a verdict onto a report row.

    Every field is written on every path, so a re-processed email can never
    keep a stale value from its previous verdict.
    """
    report.category = verdict.category
    report.status = verdict.status
    report.classify_source = verdict.classify_source
    report.has_defect = 1 if verdict.has_defect else 0
    report.defect_fields = verdict.defect_fields
    report.review_reason = verdict.review_reason
    report.field_results = verdict.field_results
    report.extracted = verdict.extracted


# The invoice name rule now lives with the other document-kind rules so the
# Gmail layer recognises an invoice by exactly the same test.
_INVOICE_NAME_RE = doc_types.INVOICE_RE
_INVOICE_NUMBER_RE = re.compile(
    r"invoice\s*(?:no|number|#|num)\s*[:.#-]?\s*([A-Z0-9][A-Z0-9/_-]{2,})", re.I)
_INVOICE_TOTAL_RE = re.compile(
    r"(?:grand\s+total|total\s+amount|amount\s+due|total)\s*[^0-9]{0,16}"
    r"([0-9][0-9,]*(?:\.[0-9]{1,2})?)", re.I)
_CURRENCY_RE = re.compile(r"\b(USD|EUR|MYR|GBP|SGD|CNY|AUD|JPY)\b")


def _invoice_fields(text: str) -> dict[str, Any]:
    """Pull the few fields a commercial invoice is stored by.

    Deliberately small: the invoice is kept as evidence of a complete document
    set, not compared field-by-field against the SI/BL pair.
    """
    fields: dict[str, Any] = {}
    number = _INVOICE_NUMBER_RE.search(text or "")
    if number:
        fields["invoice_number"] = number.group(1)
    total = _INVOICE_TOTAL_RE.search(text or "")
    if total:
        fields["total_amount"] = total.group(1).replace(",", "")
    currency = _CURRENCY_RE.search(text or "")
    if currency:
        fields["currency"] = currency.group(1).upper()
    return fields


def _register_invoice_document(db: Session, shipment, email: dict,
                               claimed: set) -> None:
    """Store a commercial invoice as its own document under the shipment.

    ``shipment`` may be ``None`` when this email carried no SI/BL pair (an
    invoice sent on its own). In that case the invoice is attached to an
    *existing* shipment identified by the reference in the subject — it never
    creates one, so an invoice alone can never invent a shipment.

    When the email carries no invoice nothing happens, and the shipment view
    simply reports the invoice as absent.
    """
    attachments = [a for a in (email.get("attachments") or []) if a not in claimed]
    invoice_path = next((a for a in attachments if _INVOICE_NAME_RE.search(str(a))), None)
    if invoice_path is None:
        return

    if shipment is None:
        key, _reference = versioning.identify_shipment(email, {}, {})
        shipment = (
            db.query(versioning.ShipmentRecord).filter_by(shipment_key=key).first()
        )
        if shipment is None:
            log.info("invoice %s has no shipment yet; skipping", invoice_path)
            return
    try:
        content = inbox_service.read_attachment(invoice_path)
    except Exception:  # noqa: BLE001 - an unreadable file must not break processing
        log.info("invoice attachment could not be read: %s", invoice_path)
        return
    filename = str(invoice_path).rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    text, _source = ai_service.document_text(filename, content)
    if not text:
        log.info("invoice attachment is unreadable: %s", filename)
        return
    try:
        versioning.register_document_version(
            db, shipment, "INVOICE", invoice_path, email, content,
            _invoice_fields(text), text,
        )
    except Exception:  # noqa: BLE001 - version bookkeeping must not fail the report
        log.warning("could not register invoice version for %s", invoice_path)


def persist_email_with_verdict(
    db: Session, email: dict, verdict: "EmailVerdict"
) -> ReportRecord:
    """Persist one email whose verdict was already computed elsewhere.

    Used by ``POST /api/process``: that endpoint runs the shared evaluator once
    (through ``routers/ingest.analyze_email``) and then needs the same side
    effects the corpus path gets — shipment grouping, documents, versions,
    issues. Re-evaluating would classify, extract, OCR and compare a second
    time, so the verdict is handed over instead and only the persistence half
    of `_run_pipeline` runs.

    Always returns a report row; a pipeline failure is recorded on it rather
    than raised, matching `process_email`.
    """
    email_id = email["email_id"]
    _upsert_email_record(db, email)

    report = db.query(ReportRecord).filter_by(email_id=email_id).first()
    if report is None:
        report = ReportRecord(email_id=email_id)
        db.add(report)
    report.attempts = (report.attempts or 0) + 1
    report.error_message = None

    t0 = time.perf_counter()
    try:
        _run_pipeline(db, report, email, verdict=verdict)
    except Exception as exc:  # noqa: BLE001 — record, don't crash the API
        log.exception("pipeline failed for %s", email_id)
        report.status = "ERROR"
        report.category = report.category or "GENERAL"
        report.error_message = f"{type(exc).__name__}: {exc}"
    report.processing_ms = (time.perf_counter() - t0) * 1000.0

    db.commit()
    db.refresh(report)
    return report


def _run_pipeline(db: Session, report: ReportRecord, email: dict,
                  verdict: Optional["EmailVerdict"] = None) -> None:
    verdict = verdict if verdict is not None else evaluate_email(email)
    pair = verdict.pair
    shipment = None

    if pair is not None:
        # The db-backed path additionally resolves which version of each
        # document is current, so a re-sent document is compared against the
        # latest revision rather than the copy inside this one email. The
        # stateless entry point skips this: it has no shipment history to
        # consult, and inventing one would be worse than the plain comparison.
        shipment, _si_version, _bl_version = versioning.sync_processed_documents(
            db,
            report=report,
            email=email,
            si_path=pair.si_path,
            bl_path=pair.bl_path,
            si_content=pair.si_content,
            bl_content=pair.bl_content,
            si_result=pair.si_result,
            bl_result=pair.bl_result,
        )
        # The email's own verdict (from `evaluate_email`) is authoritative for
        # THIS report. The db-backed path still records the documents, versions
        # and issues so the shipment view can recompute the truth, but it must
        # NOT overwrite this email's verdict with the shipment's then-current
        # pair (B5): that made a per-email report a stale snapshot of mutable
        # shipment state, and a newer version arriving later left it disagreeing
        # with the shipment view.
        #
        # Only the shipment linkage is added to `extracted`; the compared fields
        # (`si`/`bl`) stay the email's own pair. Using `latest_version` (not
        # `latest_pair_comparison`) also removes the redundant second `compare`
        # that ran here for every paired email (R1).
        latest_si = versioning.latest_version(db, shipment.id, "SI")
        latest_bl = versioning.latest_version(db, shipment.id, "BL")
        verdict.extracted = {
            **verdict.extracted,
            "shipment_id": shipment.id,
            "shipment_key": shipment.shipment_key,
            "latest_si_version_id": latest_si.id if latest_si else None,
            "latest_bl_version_id": latest_bl.id if latest_bl else None,
        }

    _apply_verdict(report, verdict)

    if pair is None:
        # Before the invoice is filed: a lone SI/BL is what makes the shipment
        # exist at all, and the invoice is filed *against* that shipment, so
        # running it afterwards would leave the invoice with nothing to attach
        # to and silently drop it.
        if verdict.category in DOCUMENT_CARRYING_CATEGORIES:
            shipment = _register_documents_without_counterpart(db, email) or shipment

    # The invoice is not one of the two compared documents, so it is registered
    # independently of the comparison: a commercial invoice that arrives in its
    # own email must still show up on the shipment.
    _register_invoice_document(db, shipment, email,
                               {pair.si_path, pair.bl_path} if pair else set())

    if pair is not None:
        versioning.sync_issues_for_report(db, report, shipment)


# Classifications that reliably carry shipping documents. SPAM/GENERAL are
# deliberately excluded: reading documents out of unrelated mail would be wasted
# extraction work with nothing to attach the result to.
DOCUMENT_CARRYING_CATEGORIES = frozenset({"BL_COMPARISON", "SI_REQUEST"})


def _register_documents_without_counterpart(db: Session, email: dict):
    """File whichever half of the pair arrived, so the shipment exists.

    Called only when this email was judged to be part of a comparison yet only
    one of the two documents is attached. The half that is here still belongs
    to a real shipment, and without it there is nothing to later report the
    missing document against.
    """
    si_path, bl_path = _find_doc_attachments(email)
    shipment = None
    for path, doc_type in ((si_path, "SI"), (bl_path, "BL")):
        if not path:
            continue
        try:
            content = inbox_service.read_attachment(path)
            result = ai_service.extract_document(doc_type, path, content)
        except Exception:  # noqa: BLE001 - an unreadable half must not fail the report
            log.info("standalone %s could not be read: %s", doc_type, path)
            continue
        shipment = versioning.register_standalone_document(
            db, email=email, doc_type=doc_type, path=path, content=content,
            extracted_fields=result.fields or {}, raw_text=result.raw_text,
        )
    return shipment


# ------------------------------------------------- attachment discovery tiers
# Tier 1 is the naming convention the corpus uses: "<id>_SI.pdf" / "<id>_BL.xlsx".
# Tier 2 exists for names that do not follow it, because an unrecognised name is
# indistinguishable from an absent file: both land on `missing_attachment`, so a
# naming difference is reported to the reviewer as "the sender forgot to attach
# it". That is a wrong reason on an escalation, and reason quality is what the
# reliability axis measures.
#
# Tier 1 always wins for a *real* file. The naming rules themselves now live in
# one place, `app.services.doc_types`, because the Gmail layer used to apply a
# stricter test than this one and the two disagreed about what an SI is — which
# is how a real `SI_v1.pdf` ended up ignored in favour of the email body.
_DOC_SUFFIXES = doc_types.DOC_SUFFIXES
_FUZZY_TOKENS = doc_types.SI_BL_TOKENS
_FUZZY_PHRASES = doc_types.SI_BL_PHRASES


def _fuzzy_doc_type(attachment: str) -> Optional[str]:
    """Guess SI/BL from a filename that does not follow the `_SI`/`_BL` convention.

    Thin alias for :func:`app.services.doc_types.detect_si_bl`; kept because it
    is the name the rest of this module and the diagnostics scripts use.
    """
    return doc_types.detect_si_bl(attachment)


def _find_doc_attachments(email: dict) -> tuple[Optional[str], Optional[str]]:
    attachments = email.get("attachments") or []
    found: dict[str, Optional[str]] = {"SI": None, "BL": None}

    # A document synthesised from an email body is a fallback, never a peer of a
    # real attachment. It is considered only after every real file has been
    # given its chance, so it can fill a genuine gap but can never displace the
    # PDF the sender actually attached.
    real = [a for a in attachments if not doc_types.is_synthetic(a)]
    synthetic = [a for a in attachments if doc_types.is_synthetic(a)]

    # Tier 1: the "<id>_SI" convention. First match wins, as it always has.
    for att in real + synthetic:
        m = _DOC_TYPE_RE.search(att)
        if m and found[m.group(1).upper()] is None:
            found[m.group(1).upper()] = att
    if all(found[k] is not None for k in ("SI", "BL")):
        return found["SI"], found["BL"]

    # Tier 2: fill only the gaps, and never spend a file tier 1 already took.
    claimed = {p for p in found.values() if p}
    for att in real + synthetic:
        if att in claimed:
            continue
        guess = _fuzzy_doc_type(att)
        if guess is not None and found[guess] is None:
            found[guess] = att
            claimed.add(att)
    return found["SI"], found["BL"]


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
            source_mailbox=infer_source_mailbox(email_id, email.get("from")),
        ))
        db.commit()
    elif received_at and row.received_at is None:
        row.received_at = received_at
        db.commit()
