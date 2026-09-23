"""Human-in-the-loop review endpoints."""
from __future__ import annotations

from typing import List

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import ReviewRecord
from app.schemas import ReviewCreate, ReviewOut
from app.services import workflow

router = APIRouter(prefix="/reviews", tags=["reviews"])


@router.post("/{email_id}", response_model=ReviewOut,
             summary="Submit a human review for an email's report")
def create_review(email_id: str, payload: ReviewCreate,
                  db: Session = Depends(get_db)):
    if workflow.inbox_service.get_email(email_id) is None:
        raise HTTPException(404, f"email not found: {email_id}")
    try:
        report = workflow.apply_review(
            db,
            email_id=email_id,
            decision=payload.decision,
            corrected_fields=payload.corrected_fields,
            corrected_category=payload.corrected_category,
            reviewer=payload.reviewer,
            notes=payload.notes,
        )
    except KeyError as exc:
        raise HTTPException(409, str(exc)) from exc

    review = (db.query(ReviewRecord)
                .filter_by(report_id=report.id)
                .order_by(ReviewRecord.id.desc())
                .first())
    return review


@router.get("", response_model=List[ReviewOut],
            summary="List all reviews")
def list_reviews(db: Session = Depends(get_db)):
    return db.query(ReviewRecord).order_by(ReviewRecord.id.desc()).all()


@router.get("/{email_id}", response_model=List[ReviewOut],
            summary="List reviews for one email")
def list_reviews_for_email(email_id: str, db: Session = Depends(get_db)):
    return (db.query(ReviewRecord)
              .filter_by(email_id=email_id)
              .order_by(ReviewRecord.id.desc())
              .all())
