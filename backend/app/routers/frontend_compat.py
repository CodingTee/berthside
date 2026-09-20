"""Frontend-compatibility router.

The P1 frontend (friend's ``app/static/index.html``) was written against a
different backend contract (``/api/summary``, ``/api/emails``, ``/api/emails/{id}``,
``/api/emails/{id}/review``, ``/api/attachments/{path}``). This router re-exposes
OUR backend through that exact contract so the friend's UI works unchanged — we
keep our engine, DB and scoring, and only translate the wire format.

No business logic lives here: every value is read from the existing
``ReportRecord`` / ``ReviewRecord`` / inbox loader. The comparison verdict and
scoring are still produced by ``services/workflow`` + ``services/comparison``.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.config import get_settings
from app.database import get_db
from app.models import ReportRecord, ReviewRecord
from app.services import inbox_service, workflow

settings = get_settings()

router = APIRouter(prefix="/api", tags=["frontend-compat"])

_CATEGORIES = ["BL_COMPARISON", "SI_REQUEST", "INVOICE_QUERY", "GENERAL", "SPAM"]


# --------------------------------------------------------------------- summary
@router.get("/summary")
def summary(db: Session = Depends(get_db)):
    """Dashboard counts consumed by the frontend KPI bar."""
    total = len(inbox_service.all_emails())
    by_status: dict[str, int] = {}
    reviewed = 0
    for r in db.query(ReportRecord).all():
        by_status[r.status or "UNKNOWN"] = by_status.get(r.status or "UNKNOWN", 0) + 1
        if r.reviewed:
            reviewed += 1
    return {"total": total, "by_status": by_status, "reviewed": reviewed}


# ---------------------------------------------------------- review queue
_REASON_META = {
    "missing_attachment": "Missing SI or BL attachment",
    "wrong_doc_type": "Attached document is the wrong type",
    "unreadable": "Attachment unreadable — needs OCR / vision / human",
    "missing_value": "Field value missing on one side (undecidable)",
}


@router.get("/review-queue")
def review_queue(db: Session = Depends(get_db)):
    """Human reviewer's triage board: every escalated (NEEDS_REVIEW) email
    grouped by *why* it was escalated. Bucketing by review_reason turns a flat
    '46 things to look at' list into actionable piles (e.g. all 'missing
    attachment' cases in one pass)."""
    emails = {e["email_id"]: e for e in inbox_service.all_emails()}
    rep_map = {r.email_id: r for r in db.query(ReportRecord).all()}

    groups: dict[str, list[dict]] = {}
    for r in rep_map.values():
        if not r or r.status != "NEEDS_REVIEW":
            continue
        reason = r.review_reason or "unknown"
        e = emails.get(r.email_id, {})
        # Short, reviewer-facing evidence pulled from the workflow run.
        extracted = r.extracted or {}
        evidence = ""
        if reason == "missing_attachment":
            evidence = ", ".join(extracted.get("missing_documents", [])) or "n/a"
        elif reason == "unreadable":
            evidence = ", ".join(extracted.get("unreadable_documents", [])) or "n/a"
        elif reason == "missing_value":
            miss = list(extracted.get("si_missing", []) or []) + \
                   list(extracted.get("bl_missing", []) or [])
            evidence = "missing: " + (", ".join(miss) if miss else "n/a")
        groups.setdefault(reason, []).append({
            "email_id": r.email_id,
            "from": e.get("from") or "",
            "subject": e.get("subject") or "",
            "category": r.category,
            "defect_fields": r.defect_fields or [],
            "evidence": evidence,
        })

    # Stable, reviewer-sensible ordering of the buckets.
    order = ["missing_attachment", "wrong_doc_type", "unreadable", "missing_value", "unknown"]
    ordered = sorted(groups.keys(), key=lambda k: (order.index(k) if k in order else 99, k))
    grouped = [{
        "reason": reason,
        "label": _REASON_META.get(reason, reason),
        "count": len(items),
        "items": sorted(items, key=lambda it: it["email_id"]),
    } for reason in ordered for items in (groups[reason],)]

    return {"total": sum(len(g["items"]) for g in grouped), "groups": grouped}


# ---------------------------------------------------------------------- list
@router.get("/emails")
def list_emails(
    category: Optional[str] = None,
    status: Optional[str] = None,
    q: Optional[str] = None,
    limit: int = Query(500, ge=1, le=500),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
):
    """Inbox list with the same filters the UI sends."""
    emails = inbox_service.all_emails()
    rep_map = {r.email_id: r for r in db.query(ReportRecord).all()}

    items = []
    for e in emails:
        eid = e["email_id"]
        r = rep_map.get(eid)
        if category and (not r or r.category != category):
            continue
        if status and (not r or r.status != status):
            continue
        if q:
            hay = f"{eid} {e.get('from', '')} {e.get('subject', '')}".lower()
            if q.lower() not in hay:
                continue
        items.append({
            "email_id": eid,
            "from": e.get("from") or "",
            "subject": e.get("subject") or "",
            "n_attachments": len(e.get("attachments") or []),
            "category": r.category if r else None,
            "status": r.status if r else None,
            "has_defect": bool(r.has_defect) if r else False,
            "defect_fields": (r.defect_fields or []) if r else [],
            "review_reason": r.review_reason if r else None,
            "decided_by": "human" if (r and r.reviewed) else "rule",
        })

    total = len(items)
    return {"total": total, "items": items[offset:offset + limit]}


# -------------------------------------------------------------------- detail
@router.get("/emails/{email_id}")
def email_detail(email_id: str, db: Session = Depends(get_db)):
    """Full detail incl. the SI vs BL field comparison the UI renders.

    Strictly read-only. This used to run the whole pipeline whenever no report
    existed yet, so a *read* performed classification, OCR, extraction and
    verification and then wrote a report — meaning a refresh could change what
    the page showed, and a Dashboard read was doing document processing.
    It now reports ``processed: false`` with an empty result; running the
    pipeline is the explicit ``POST /emails/{id}/process`` endpoint's job.
    """
    email = inbox_service.get_email(email_id)
    if email is None:
        raise HTTPException(404, f"email not found: {email_id}")

    report = db.query(ReportRecord).filter_by(email_id=email_id).first()

    comparisons = []
    for fr in ((report.field_results or []) if report else []):
        si = fr.get("si_value")
        bl = fr.get("bl_value")
        comparisons.append({
            "field": fr.get("field"),
            "si_value": si,
            "bl_value": bl,
            "match": fr.get("match"),
            "missing": (si is None or bl is None),
        })

    human_review = None
    hr = (db.query(ReviewRecord).filter_by(email_id=email_id)
          .order_by(ReviewRecord.id.desc()).first())
    if hr:
        human_review = {
            "action": "confirm" if hr.decision == "CONFIRM" else "override",
            "status": report.status if report else None,
            "note": hr.notes,
        }

    received = email.get("received_at")
    return {
        "email": {
            "email_id": email_id,
            "from": email.get("from") or "",
            "subject": email.get("subject") or "",
            "body": email.get("body") or "",
            "attachments": email.get("attachments") or [],
            "received_at": (received.isoformat()
                            if hasattr(received, "isoformat") else received),
        },
        # Lets the UI distinguish "not processed yet" from "processed, no
        # issues" without having to infer it from an empty result.
        "processed": report is not None,
        "result": {
            "category": report.category,
            "status": report.status,
            "review_reason": report.review_reason,
            "has_defect": bool(report.has_defect),
            "defect_fields": report.defect_fields or [],
            "decided_by": "human" if report.reviewed else "rule",
            "rule": None,
        } if report else None,
        "comparisons": comparisons,
        "human_review": human_review,
    }


# -------------------------------------------------------------------- review
class FrontendReviewIn(BaseModel):
    action: str                       # confirm | override
    status: Optional[str] = None      # override target: OK | MISMATCH | NEEDS_REVIEW
    defect_fields: Optional[list[str]] = None
    note: Optional[str] = None


@router.post("/emails/{email_id}/review")
def review(email_id: str, payload: FrontendReviewIn,
           db: Session = Depends(get_db)):
    report = db.query(ReportRecord).filter_by(email_id=email_id).first()
    if report is None:
        report = workflow.process_email(db, email_id)

    decision = "CONFIRM" if payload.action == "confirm" else "CORRECT"
    db.add(ReviewRecord(
        report_id=report.id,
        email_id=email_id,
        reviewer="human",
        decision=decision,
        corrected_fields=payload.defect_fields or {},
        notes=payload.note,
    ))
    report.reviewed = 1

    if payload.action == "override" and payload.status:
        report.status = payload.status
        report.has_defect = 1 if payload.status == "MISMATCH" else 0
        if payload.status != "MISMATCH":
            report.defect_fields = []
        else:
            report.defect_fields = payload.defect_fields or report.defect_fields or []
        if payload.status != "NEEDS_REVIEW":
            report.review_reason = None

    db.commit()
    return {
        "ok": True,
        "result": {
            "email_id": email_id,
            "category": report.category,
            "status": report.status,
            "has_defect": bool(report.has_defect),
            "defect_fields": report.defect_fields or [],
            "decided_by": "human",
        },
    }


# ---------------------------------------------------------------- attachments
@router.get("/attachments/{rel_path:path}")
def attachment(rel_path: str):
    """Serve an attachment file (path-traversal safe).

    ``rel_path`` is exactly the string in ``email['attachments']``
    (e.g. ``attachments/email_004_SI.txt``), resolved against DATA_SOURCE.
    """
    base = Path(settings.data_source).resolve()
    full = (base / rel_path).resolve()
    if not str(full).startswith(str(base)) or not full.is_file():
        raise HTTPException(404, "attachment not found")
    return FileResponse(full)
