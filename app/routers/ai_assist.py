"""Optional AI-enhancement endpoints.

These endpoints are intentionally separate from the deterministic verification
workflow. They generate reviewer-facing assistance from stored issue data and
never mutate official documents or send communication automatically.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.database import get_db
from app.schemas import (
    AIAmbiguousInterpretationIn,
    AIAmbiguousInterpretationOut,
    AIEmailDraftOut,
    AIMismatchAssistOut,
)
from app.services import ai_enhancement

router = APIRouter(prefix="/ai", tags=["ai-assist"])


@router.get("/issues/{issue_id}/explain", response_model=AIMismatchAssistOut,
            summary="Generate AI/rule-assisted mismatch explanation")
def explain_issue(issue_id: int, db: Session = Depends(get_db)):
    try:
        return ai_enhancement.mismatch_assistance(db, issue_id)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc


@router.get("/issues/{issue_id}/suggest", response_model=AIMismatchAssistOut,
            summary="Generate AI/rule-assisted correction suggestion")
def suggest_issue(issue_id: int, db: Session = Depends(get_db)):
    try:
        return ai_enhancement.mismatch_assistance(db, issue_id)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc


@router.get("/shipments/{shipment_id}/correction-email",
            response_model=AIEmailDraftOut,
            summary="Draft a correction request email for human review")
def correction_email(shipment_id: int, db: Session = Depends(get_db)):
    try:
        return ai_enhancement.correction_email_draft(db, shipment_id)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc


@router.post("/ambiguous-interpretation",
             response_model=AIAmbiguousInterpretationOut,
             summary="Assist with ambiguous OCR-like text")
def ambiguous_interpretation(payload: AIAmbiguousInterpretationIn):
    return ai_enhancement.ambiguous_interpretation(
        text=payload.text,
        field_name=payload.field_name,
    )
