"""Email endpoints: list, detail, process, process-all."""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.database import get_db
from app.schemas import (EmailListOut, EmailOut, ProcessAllResponse,
                          ProcessResponse)
from app.services import inbox_service, workflow

router = APIRouter(prefix="/emails", tags=["emails"])


def _email_out(email: dict) -> EmailOut:
    return EmailOut(
        email_id=email["email_id"],
        **{"from": email.get("from") or ""},
        subject=email.get("subject") or "",
        body=email.get("body") or "",
        attachments=email.get("attachments") or [],
        has_attachments=bool(email.get("attachments")),
    )


@router.get("", response_model=EmailListOut, summary="List inbox emails")
def list_emails(
    limit: int = Query(50, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
):
    """Inbox listing enriched with processing status from the DB."""
    from app.models import ReportRecord

    emails = inbox_service.all_emails()
    total = len(emails)
    page = emails[offset:offset + limit]

    statuses = {
        r.email_id: (r.category, r.status)
        for r in db.query(ReportRecord.email_id, ReportRecord.category,
                          ReportRecord.status).all()
    }

    items = []
    for e in page:
        cat, status = statuses.get(e["email_id"], (None, None))
        items.append({
            "email_id": e["email_id"],
            "from": e.get("from") or "",
            "subject": e.get("subject") or "",
            "attachments": e.get("attachments") or [],
            "has_attachments": bool(e.get("attachments")),
            "processed": e["email_id"] in statuses,
            "status": status,
        })

    return EmailListOut(total=total, limit=limit, offset=offset, emails=items)


@router.get("/{email_id}", response_model=EmailOut, summary="Get one email")
def get_email(email_id: str):
    email = inbox_service.get_email(email_id)
    if email is None:
        raise HTTPException(404, f"email not found: {email_id}")
    return _email_out(email)


@router.post("/{email_id}/process", response_model=ProcessResponse,
             summary="Run the pipeline for one email (idempotent; re-run = retry)")
def process_email(email_id: str, db: Session = Depends(get_db)):
    email = inbox_service.get_email(email_id)
    if email is None:
        raise HTTPException(404, f"email not found: {email_id}")

    report = workflow.process_email(db, email_id)
    return ProcessResponse(
        email_id=report.email_id,
        category=report.category,
        status=report.status,
        has_defect=bool(report.has_defect),
        defect_fields=report.defect_fields or [],
        review_reason=report.review_reason,
        field_results=report.field_results or [],
        error_message=report.error_message,
        attempts=report.attempts or 1,
        processing_ms=report.processing_ms,
    )


@router.post("/process-all", response_model=ProcessAllResponse,
             summary="Process the entire inbox")
def process_all(
    limit: int = Query(0, ge=0, le=10000),
    db: Session = Depends(get_db),
):
    stats = workflow.process_all(db, limit=limit)
    return ProcessAllResponse(**stats)


@router.post("/retry-failed",
             summary="Retry every email that ended in ERROR / NEEDS_REVIEW")
def retry_failed(
    statuses: list[str] = Query(["ERROR", "NEEDS_REVIEW"]),
    limit: int = Query(0, ge=0, le=10000),
    db: Session = Depends(get_db),
):
    """Visible failures + retries: re-runs the pipeline for the emails whose
    last attempt did not produce a verdict. Idempotent — safe to call often."""
    return workflow.retry_failed(db, statuses=statuses, limit=limit)


@router.get("/{email_id}/status",
            summary="Processing status for one email (PENDING / COMPLETED / "
                    "NEEDS_HUMAN / FAILED)")
def email_status(email_id: str, db: Session = Depends(get_db)):
    if inbox_service.get_email(email_id) is None:
        raise HTTPException(404, f"email not found: {email_id}")
    return workflow.get_status(db, email_id)
