"""Shipment, version-control, and smart-resolution endpoints."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import (
    DocumentRecord,
    DocumentVersionRecord,
    IssueRecord,
    ResolutionRecord,
    ShipmentRecord,
)
from app.schemas import (
    DocumentOut,
    DocumentVersionOut,
    IssueDetailOut,
    IssueOut,
    ResolutionOut,
    ResolutionReviewIn,
    ResolutionStatusOut,
    ShipmentDetailOut,
    ShipmentListOut,
    ShipmentOverviewListOut,
    ShipmentOverviewOut,
    ShipmentSummaryOut,
    VersionDiffOut,
    VersionFieldDiff,
)
from app.services import shipment_overview, versioning

router = APIRouter(tags=["shipments"])


def _version_out(row: DocumentVersionRecord | None) -> DocumentVersionOut | None:
    if row is None:
        return None
    data = DocumentVersionOut.model_validate(row)
    data.is_latest = bool(row.is_latest)
    return data


def _shipment_summary(db: Session, shipment: ShipmentRecord) -> ShipmentSummaryOut:
    # One authoritative status, computed once. This used to report
    # `resolution_status` in `status` *and* the document-aware status in
    # `shipment_status`, so the same payload could say VERIFIED and
    # NEEDS_ATTENTION about the same shipment. `status` now carries the
    # authoritative value; the issue-resolution counters stay for the
    # resolution workflow, which is a different question.
    overview = shipment_overview.build_overview(db, shipment)
    resolution = versioning.resolution_status(db, shipment.id)
    return ShipmentSummaryOut(
        id=shipment.id,
        shipment_key=shipment.shipment_key,
        reference_number=shipment.reference_number,
        status=overview["status"],
        resolution_state=resolution["status"],
        si_latest=_version_out(versioning.latest_version(db, shipment.id, "SI")),
        bl_latest=_version_out(versioning.latest_version(db, shipment.id, "BL")),
        mismatch_count=resolution["total_issues"],
        pending_count=resolution["pending_approval"],
        resolved_count=resolution["resolved"],
        shipment_code=overview["shipment_id"],
        shipment_status=overview["status"],
        document_types=overview["document_types"],
        missing_documents=overview["missing_documents"],
        complete=overview["complete"],
        reasons=overview["reasons"],
        actions=overview["actions"],
    )


@router.get("/shipments", response_model=ShipmentListOut,
            summary="List shipment groups")
def list_shipments(db: Session = Depends(get_db)):
    rows = db.query(ShipmentRecord).order_by(ShipmentRecord.id.desc()).all()
    return ShipmentListOut(
        total=len(rows),
        shipments=[_shipment_summary(db, row) for row in rows],
    )


@router.get("/shipments/overview", response_model=ShipmentOverviewListOut,
            summary="Shipment-centred list: status, completeness, issues, actions")
def list_shipment_overviews(db: Session = Depends(get_db)):
    rows = db.query(ShipmentRecord).order_by(ShipmentRecord.id.desc()).all()
    return ShipmentOverviewListOut(
        total=len(rows),
        shipments=[ShipmentOverviewOut(**shipment_overview.build_overview(db, row))
                   for row in rows],
    )


@router.get("/shipments/by-key/{shipment_key}", response_model=ShipmentOverviewOut,
            summary="Shipment overview by business code (e.g. SHP-002)")
def get_shipment_by_key(shipment_key: str, db: Session = Depends(get_db)):
    """Look a shipment up the way a human refers to it.

    Accepts ``SHP-002``, ``REF:SHP-002`` or the raw shipment key, because the
    stored key carries a prefix that the business code does not.
    """
    key = (shipment_key or "").strip()
    candidates = [key, f"REF:{key.upper()}", key.upper(), f"REF:{key}"]
    row = None
    for candidate in candidates:
        row = db.query(ShipmentRecord).filter_by(shipment_key=candidate).first()
        if row is not None:
            break
    if row is None:
        row = (
            db.query(ShipmentRecord)
            .filter(ShipmentRecord.shipment_key.ilike(f"%{key}"))
            .first()
        )
    if row is None:
        raise HTTPException(404, f"shipment '{shipment_key}' not found")
    return ShipmentOverviewOut(**shipment_overview.build_overview(db, row))


@router.get("/shipments/{shipment_id}", response_model=ShipmentDetailOut,
            summary="Get one shipment with documents and versions")
def get_shipment(shipment_id: int, db: Session = Depends(get_db)):
    shipment = db.query(ShipmentRecord).filter_by(id=shipment_id).first()
    if shipment is None:
        raise HTTPException(404, "shipment not found")

    documents = []
    for doc in (
        db.query(DocumentRecord)
        .filter_by(shipment_id=shipment_id)
        .order_by(DocumentRecord.doc_type)
        .all()
    ):
        versions = (
            db.query(DocumentVersionRecord)
            .filter_by(document_id=doc.id)
            .order_by(DocumentVersionRecord.version_number, DocumentVersionRecord.id)
            .all()
        )
        documents.append(DocumentOut(
            id=doc.id,
            shipment_id=doc.shipment_id,
            doc_type=doc.doc_type,
            document_key=doc.document_key,
            versions=[_version_out(v) for v in versions],
        ))

    summary = _shipment_summary(db, shipment)
    return ShipmentDetailOut(**summary.model_dump(), documents=documents)


@router.get("/shipments/{shipment_id}/overview", response_model=ShipmentOverviewOut,
            summary="Shipment-centred detail: documents, versions, issues, sources")
def get_shipment_overview(shipment_id: int, db: Session = Depends(get_db)):
    shipment = db.query(ShipmentRecord).filter_by(id=shipment_id).first()
    if shipment is None:
        raise HTTPException(404, "shipment not found")
    # Read-only: everything below comes from stored versions, reports and
    # issues. No classification, extraction or verification runs here.
    return ShipmentOverviewOut(**shipment_overview.build_overview(db, shipment))


@router.get("/shipments/{shipment_id}/documents", response_model=list[DocumentOut],
            summary="List documents for a shipment")
def list_documents(shipment_id: int, db: Session = Depends(get_db)):
    if db.query(ShipmentRecord).filter_by(id=shipment_id).first() is None:
        raise HTTPException(404, "shipment not found")
    return get_shipment(shipment_id, db).documents


@router.get("/shipments/{shipment_id}/versions",
            response_model=list[DocumentVersionOut],
            summary="List document versions for a shipment")
def list_versions(
    shipment_id: int,
    doc_type: str | None = Query(None, pattern="^(SI|BL|INVOICE)$"),
    db: Session = Depends(get_db),
):
    q = db.query(DocumentVersionRecord).filter_by(shipment_id=shipment_id)
    if doc_type:
        q = q.filter_by(doc_type=doc_type)
    rows = q.order_by(
        DocumentVersionRecord.doc_type,
        DocumentVersionRecord.version_number,
        DocumentVersionRecord.id,
    ).all()
    return [_version_out(v) for v in rows]


@router.get("/shipments/{shipment_id}/version-diff",
            response_model=VersionDiffOut,
            summary="Compare two document versions field by field")
def get_version_diff(
    shipment_id: int,
    from_version_id: int = Query(...),
    to_version_id: int = Query(...),
    db: Session = Depends(get_db),
):
    from_v = db.query(DocumentVersionRecord).filter_by(id=from_version_id).first()
    to_v = db.query(DocumentVersionRecord).filter_by(id=to_version_id).first()
    if not from_v or not to_v or from_v.shipment_id != shipment_id or to_v.shipment_id != shipment_id:
        raise HTTPException(404, "version not found for shipment")
    fields = [
        VersionFieldDiff(**row)
        for row in versioning.version_diff(db, from_version_id, to_version_id)
    ]
    return VersionDiffOut(
        shipment_id=shipment_id,
        from_version_id=from_version_id,
        to_version_id=to_version_id,
        fields=fields,
    )


@router.get("/shipments/{shipment_id}/issues", response_model=list[IssueOut],
            summary="List active issues for a shipment")
def list_shipment_issues(shipment_id: int, db: Session = Depends(get_db)):
    return (
        db.query(IssueRecord)
        .filter_by(shipment_id=shipment_id)
        .filter(IssueRecord.status != "SUPERSEDED")
        .order_by(IssueRecord.id)
        .all()
    )


@router.get("/issues/{issue_id}", response_model=IssueDetailOut,
            summary="Get issue and its resolution record")
def get_issue(issue_id: int, db: Session = Depends(get_db)):
    issue = db.query(IssueRecord).filter_by(id=issue_id).first()
    if issue is None:
        raise HTTPException(404, "issue not found")
    resolution = db.query(ResolutionRecord).filter_by(issue_id=issue.id).first()
    return IssueDetailOut(
        **IssueOut.model_validate(issue).model_dump(),
        resolution=ResolutionOut.model_validate(resolution) if resolution else None,
    )


@router.post("/issues/{issue_id}/suggest", response_model=ResolutionOut,
             summary="Create or refresh a deterministic suggested correction")
def suggest_issue(issue_id: int, db: Session = Depends(get_db)):
    try:
        return versioning.suggest_resolution(db, issue_id)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc


@router.post("/issues/{issue_id}/approve", response_model=ResolutionOut,
             summary="Approve a suggested correction")
def approve_issue(
    issue_id: int,
    payload: ResolutionReviewIn | None = None,
    db: Session = Depends(get_db),
):
    payload = payload or ResolutionReviewIn()
    try:
        return versioning.approve_resolution(
            db, issue_id, payload.reviewed_by, payload.review_comment
        )
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc


@router.post("/issues/{issue_id}/reject", response_model=ResolutionOut,
             summary="Reject a suggested correction")
def reject_issue(
    issue_id: int,
    payload: ResolutionReviewIn | None = None,
    db: Session = Depends(get_db),
):
    payload = payload or ResolutionReviewIn()
    try:
        return versioning.reject_resolution(
            db, issue_id, payload.reviewed_by, payload.review_comment
        )
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc


@router.post("/shipments/{shipment_id}/generate-corrected-draft",
             summary="Generate a corrected BL draft from approved resolutions")
def generate_corrected_draft(shipment_id: int, db: Session = Depends(get_db)):
    try:
        return versioning.generate_corrected_draft(db, shipment_id)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@router.get("/shipments/{shipment_id}/resolution-status",
            response_model=ResolutionStatusOut,
            summary="Get shipment resolution status")
def get_resolution_status(shipment_id: int, db: Session = Depends(get_db)):
    if db.query(ShipmentRecord).filter_by(id=shipment_id).first() is None:
        raise HTTPException(404, "shipment not found")
    return versioning.resolution_status(db, shipment_id)
