"""Report endpoints: list, detail (by report id OR email id), submission."""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import ReportRecord
from app.schemas import (ReportListOut, ReportOut, ReportSummary,
                         SubmissionEntry, SubmissionOut)
from app.services import inbox_service
from app.services.submission import submission_entry

router = APIRouter(prefix="/reports", tags=["reports"])


def _summary(r: ReportRecord) -> ReportSummary:
    return ReportSummary(
        report_id=r.id,
        email_id=r.email_id,
        category=r.category,
        status=r.status,
        has_defect=bool(r.has_defect),
        defect_fields=r.defect_fields or [],
        review_reason=r.review_reason,
    )


@router.get("", response_model=ReportListOut, summary="List stored reports")
def list_reports(
    status: Optional[str] = Query(None),
    category: Optional[str] = Query(None),
    has_defect: Optional[bool] = Query(None),
    limit: int = Query(50, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
):
    q = db.query(ReportRecord)
    if status:
        q = q.filter(ReportRecord.status == status.upper())
    if category:
        q = q.filter(ReportRecord.category == category.upper())
    if has_defect is not None:
        q = q.filter(ReportRecord.has_defect == (1 if has_defect else 0))

    total = q.count()
    rows = (q.order_by(ReportRecord.id)
             .offset(offset).limit(limit).all())
    return ReportListOut(
        total=total, limit=limit, offset=offset,
        reports=[_summary(r) for r in rows],
    )


@router.get("/summary/stats", summary="Aggregate stats over stored reports")
def stats(db: Session = Depends(get_db)):
    rows = db.query(ReportRecord).all()
    by_status: dict[str, int] = {}
    by_category: dict[str, int] = {}
    defect_counter: dict[str, int] = {}
    for r in rows:
        by_status[r.status or "?"] = by_status.get(r.status or "?", 0) + 1
        by_category[r.category or "?"] = by_category.get(r.category or "?", 0) + 1
        for f in (r.defect_fields or []):
            defect_counter[f] = defect_counter.get(f, 0) + 1
    return {
        "total_reports": len(rows),
        "by_status": by_status,
        "by_category": by_category,
        "defect_fields_frequency": defect_counter,
        "needs_review": by_status.get("NEEDS_REVIEW", 0),
        "errors": by_status.get("ERROR", 0),
    }


@router.get("/submission/json", response_model=SubmissionOut,
            summary="Self-evaluation submission (sample_submission.json shape)")
def submission(db: Session = Depends(get_db)):
    """Every email in the dataset must appear, exactly once, keyed by
    email_id — unprocessed emails default to GENERAL/OK like the sample."""
    sample = inbox_service.sample_submission()
    defaults: dict = {k: v for k, v in sample.items()}

    rows = db.query(ReportRecord).all()
    reports = {r.email_id: r for r in rows}

    out: dict[str, SubmissionEntry] = {}
    for email_id in defaults:
        r = reports.get(email_id)
        if r is None:
            base = defaults[email_id]
            out[email_id] = SubmissionEntry(**base)
            continue
        out[email_id] = SubmissionEntry(**submission_entry(r))

    return SubmissionOut(
        submission=out,
        emails_covered=len(out),
        total_inbox=len(defaults),
    )


@router.get("/{report_id}", response_model=ReportOut,
             response_model_by_alias=False,
             summary="Get a report by numeric report id or by email id")
def get_report(report_id: str, db: Session = Depends(get_db)):
    report: Optional[ReportRecord] = None
    if report_id.isdigit():
        report = db.query(ReportRecord).filter_by(id=int(report_id)).first()
    if report is None:  # treat the path as an email id
        report = db.query(ReportRecord).filter_by(email_id=report_id).first()
    if report is None:
        raise HTTPException(404, f"report not found: {report_id}")
    return report
