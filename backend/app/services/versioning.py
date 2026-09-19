"""Deterministic shipment versioning and resolution helpers.

This module deliberately does not call an AI provider. It organizes documents,
selects the latest active version by chronology, creates structured issue
records from deterministic comparisons, and records human-approved resolution
actions without modifying original attachments.
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from sqlalchemy.orm import Session

from app.models import (
    DocumentRecord,
    DocumentVersionRecord,
    IssueRecord,
    ResolutionRecord,
    ShipmentRecord,
    utcnow,
)
from app.schemas import COMPARED_FIELDS
from app.services.comparison import compare
from app.services.extractor import normalize

REFERENCE_RE = re.compile(
    r"\b(?:SDOC[-_ ]?)?\d{3,8}\b|\b[A-Z0-9]{3,8}[-_/][A-Z0-9]{3,8}\b",
    re.IGNORECASE,
)


def sync_processed_documents(
    db: Session,
    *,
    report,
    email: dict,
    si_path: str,
    bl_path: str,
    si_content: bytes,
    bl_content: bytes,
    si_result,
    bl_result,
) -> tuple[ShipmentRecord, DocumentVersionRecord, DocumentVersionRecord]:
    """Register SI/BL documents and return the shipment plus created versions."""
    shipment_key, reference = identify_shipment(email, si_result.fields, bl_result.fields)
    shipment = _get_or_create_shipment(db, shipment_key, reference)
    si_version = register_document_version(
        db, shipment, "SI", si_path, email, si_content, si_result.fields,
        si_result.raw_text,
    )
    bl_version = register_document_version(
        db, shipment, "BL", bl_path, email, bl_content, bl_result.fields,
        bl_result.raw_text,
    )
    db.flush()
    return shipment, si_version, bl_version


def identify_shipment(
    email: dict,
    si_fields: dict[str, Any],
    bl_fields: dict[str, Any],
) -> tuple[str, Optional[str]]:
    """Infer a stable shipment key from references first, field signature second."""
    haystack = " ".join(
        str(x or "")
        for x in [
            email.get("subject"),
            email.get("body"),
            email.get("email_id"),
            " ".join(email.get("attachments") or []),
        ]
    )
    reference = _first_reference(haystack)
    if reference:
        return f"REF:{reference.upper()}", reference.upper()

    merged = {**(bl_fields or {}), **(si_fields or {})}
    stable_parts = [
        normalize("shipper", merged.get("shipper")),
        normalize("port_of_loading", merged.get("port_of_loading")),
        normalize("port_of_discharge", merged.get("port_of_discharge")),
    ]
    if all(stable_parts):
        digest = hashlib.sha256("|".join(map(str, stable_parts)).encode()).hexdigest()[:16]
        return f"SIG:{digest}", None

    return f"EMAIL:{email.get('email_id')}", None


def _first_reference(text: str) -> Optional[str]:
    match = REFERENCE_RE.search(text or "")
    return match.group(0).replace(" ", "-") if match else None


def _get_or_create_shipment(
    db: Session, shipment_key: str, reference: Optional[str]
) -> ShipmentRecord:
    row = db.query(ShipmentRecord).filter_by(shipment_key=shipment_key).first()
    if row:
        if reference and not row.reference_number:
            row.reference_number = reference
        return row
    row = ShipmentRecord(
        shipment_key=shipment_key,
        reference_number=reference,
        status="NEEDS_REVIEW",
    )
    db.add(row)
    db.flush()
    return row


def register_document_version(
    db: Session,
    shipment: ShipmentRecord,
    doc_type: str,
    filename: str,
    email: dict,
    content: bytes,
    extracted_fields: dict[str, Any],
    raw_text: str,
) -> DocumentVersionRecord:
    """Create a new version only when the document changed.

    Byte-identical or normalized-content-identical documents are recorded as
    duplicates for auditability, but they do not advance the version number or
    become latest.
    """
    document = _get_or_create_document(db, shipment.id, doc_type)
    content_hash = hashlib.sha256(content or b"").hexdigest()
    normalized_hash = _normalized_hash(extracted_fields)
    received_at = email.get("received_at")
    if isinstance(received_at, str):
        try:
            received_at = datetime.fromisoformat(received_at.replace("Z", "+00:00"))
        except ValueError:
            received_at = None

    duplicate = (
        db.query(DocumentVersionRecord)
        .filter_by(document_id=document.id)
        .filter(
            (DocumentVersionRecord.content_hash == content_hash)
            | (DocumentVersionRecord.normalized_hash == normalized_hash)
        )
        .order_by(DocumentVersionRecord.id)
        .first()
    )
    latest = latest_version(db, shipment.id, doc_type)

    if duplicate:
        row = DocumentVersionRecord(
            shipment_id=shipment.id,
            document_id=document.id,
            doc_type=doc_type,
            filename=filename,
            email_id=email.get("email_id"),
            version_number=duplicate.version_number,
            previous_version_id=latest.id if latest else None,
            duplicate_of_version_id=duplicate.id,
            is_latest=0,
            document_status="DUPLICATE",
            content_hash=content_hash,
            normalized_hash=normalized_hash,
            extracted_fields=extracted_fields or {},
            raw_text_preview=(raw_text or "")[:2000],
            received_at=received_at,
        )
        db.add(row)
        db.flush()
        return row

    next_number = _next_version_number(db, document.id)
    if latest:
        latest.is_latest = 0
    row = DocumentVersionRecord(
        shipment_id=shipment.id,
        document_id=document.id,
        doc_type=doc_type,
        filename=filename,
        email_id=email.get("email_id"),
        version_number=next_number,
        previous_version_id=latest.id if latest else None,
        is_latest=1,
        document_status="ACTIVE",
        content_hash=content_hash,
        normalized_hash=normalized_hash,
        extracted_fields=extracted_fields or {},
        raw_text_preview=(raw_text or "")[:2000],
        received_at=received_at,
    )
    db.add(row)
    db.flush()
    return row


def _get_or_create_document(db: Session, shipment_id: int, doc_type: str) -> DocumentRecord:
    document_key = f"{shipment_id}:{doc_type}"
    row = (
        db.query(DocumentRecord)
        .filter_by(shipment_id=shipment_id, doc_type=doc_type, document_key=document_key)
        .first()
    )
    if row:
        return row
    row = DocumentRecord(
        shipment_id=shipment_id,
        doc_type=doc_type,
        document_key=document_key,
    )
    db.add(row)
    db.flush()
    return row


def _normalized_hash(fields: dict[str, Any]) -> str:
    canonical = {
        f: normalize(f, (fields or {}).get(f))
        for f in COMPARED_FIELDS
        if (fields or {}).get(f) is not None
    }
    payload = json.dumps(canonical, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _next_version_number(db: Session, document_id: int) -> int:
    rows = (
        db.query(DocumentVersionRecord.version_number)
        .filter_by(document_id=document_id, document_status="ACTIVE")
        .all()
    )
    if not rows:
        return 1
    return max(row[0] for row in rows) + 1


def latest_version(
    db: Session, shipment_id: int, doc_type: str
) -> Optional[DocumentVersionRecord]:
    return (
        db.query(DocumentVersionRecord)
        .filter_by(shipment_id=shipment_id, doc_type=doc_type, is_latest=1)
        .order_by(DocumentVersionRecord.id.desc())
        .first()
    )


def latest_pair_comparison(db: Session, shipment_id: int):
    si = latest_version(db, shipment_id, "SI")
    bl = latest_version(db, shipment_id, "BL")
    if not si or not bl:
        return None, si, bl
    return compare(si.extracted_fields or {}, bl.extracted_fields or {}, COMPARED_FIELDS), si, bl


def version_diff(db: Session, from_version_id: int, to_version_id: int) -> list[dict]:
    from_v = db.query(DocumentVersionRecord).filter_by(id=from_version_id).first()
    to_v = db.query(DocumentVersionRecord).filter_by(id=to_version_id).first()
    if from_v is None or to_v is None:
        raise KeyError("version not found")
    diff = []
    for field in COMPARED_FIELDS:
        a = (from_v.extracted_fields or {}).get(field)
        b = (to_v.extracted_fields or {}).get(field)
        diff.append({
            "field": field,
            "from_value": a,
            "to_value": b,
            "changed": normalize(field, a) != normalize(field, b),
        })
    return diff


def sync_issues_for_report(db: Session, report, shipment: ShipmentRecord) -> None:
    """Create/update issue and suggested-resolution records for mismatches."""
    db.flush()
    current_fields = {
        result["field"]
        for result in (report.field_results or [])
        if result.get("match") is False
    }
    stale = (
        db.query(IssueRecord)
        .filter_by(shipment_id=shipment.id)
        .filter(IssueRecord.status.in_(["OPEN", "SUGGESTED", "PENDING_APPROVAL"]))
        .all()
    )
    for issue in stale:
        if issue.field_name not in current_fields:
            issue.status = "SUPERSEDED"
            resolution = db.query(ResolutionRecord).filter_by(issue_id=issue.id).first()
            if resolution and resolution.status in ("SUGGESTED", "PENDING_APPROVAL"):
                resolution.status = "SUPERSEDED"

    for result in report.field_results or []:
        if result.get("match") is not False:
            continue
        field = result["field"]
        issue_key = f"{report.id}:{field}"
        issue = db.query(IssueRecord).filter_by(issue_key=issue_key).first()
        si_value = result.get("si_value")
        bl_value = result.get("bl_value")
        if issue is None:
            issue = IssueRecord(
                issue_key=issue_key,
                shipment_id=shipment.id,
                report_id=report.id,
                email_id=report.email_id,
                field_name=field,
            )
            db.add(issue)
        issue.si_value = si_value
        issue.bl_value = bl_value
        issue.difference = _difference(field, si_value, bl_value)
        issue.explanation = deterministic_explanation(field, si_value, bl_value)
        if issue.status in (None, "OPEN"):
            issue.status = "SUGGESTED"
        db.flush()
        _ensure_resolution(db, issue)

    _update_shipment_status(db, shipment.id)


def _ensure_resolution(db: Session, issue: IssueRecord) -> ResolutionRecord:
    resolution = db.query(ResolutionRecord).filter_by(issue_id=issue.id).first()
    if resolution is None:
        resolution = ResolutionRecord(
            issue_id=issue.id,
            shipment_id=issue.shipment_id,
            report_id=issue.report_id,
            field_name=issue.field_name,
            suggested_action=f"UPDATE_BL_{issue.field_name.upper()}",
            status="PENDING_APPROVAL",
        )
        db.add(resolution)
    resolution.original_si_value = issue.si_value
    resolution.original_bl_value = issue.bl_value
    resolution.suggested_value = issue.si_value
    return resolution


def deterministic_explanation(field: str, si_value: Any, bl_value: Any) -> str:
    label = field.replace("_", " ")
    diff = _difference(field, si_value, bl_value)
    if diff:
        return f"BL {label} differs from SI by {diff}."
    return f"BL {label} differs from the SI value."


def _difference(field: str, si_value: Any, bl_value: Any) -> Optional[str]:
    if field == "gross_weight_kg":
        si = normalize(field, si_value)
        bl = normalize(field, bl_value)
        if si is not None and bl is not None:
            return f"{abs(float(bl) - float(si)):g} kg"
    if field == "container_count":
        si = normalize(field, si_value)
        bl = normalize(field, bl_value)
        if si is not None and bl is not None:
            return f"{abs(int(bl) - int(si))} container(s)"
    return None


def suggest_resolution(db: Session, issue_id: int) -> ResolutionRecord:
    issue = db.query(IssueRecord).filter_by(id=issue_id).first()
    if issue is None:
        raise KeyError("issue not found")
    issue.status = "SUGGESTED"
    resolution = _ensure_resolution(db, issue)
    resolution.status = "PENDING_APPROVAL"
    db.commit()
    db.refresh(resolution)
    return resolution


def approve_resolution(
    db: Session, issue_id: int, reviewed_by: str, review_comment: Optional[str]
) -> ResolutionRecord:
    resolution = _resolution_for_issue(db, issue_id)
    resolution.status = "APPROVED"
    resolution.reviewed_by = reviewed_by
    resolution.review_comment = review_comment
    resolution.reviewed_at = utcnow()
    issue = db.query(IssueRecord).filter_by(id=issue_id).first()
    if issue:
        issue.status = "APPROVED"
        _update_shipment_status(db, issue.shipment_id)
    db.commit()
    db.refresh(resolution)
    return resolution


def reject_resolution(
    db: Session, issue_id: int, reviewed_by: str, review_comment: Optional[str]
) -> ResolutionRecord:
    resolution = _resolution_for_issue(db, issue_id)
    resolution.status = "REJECTED"
    resolution.reviewed_by = reviewed_by
    resolution.review_comment = review_comment
    resolution.reviewed_at = utcnow()
    issue = db.query(IssueRecord).filter_by(id=issue_id).first()
    if issue:
        issue.status = "REJECTED"
        _update_shipment_status(db, issue.shipment_id)
    db.commit()
    db.refresh(resolution)
    return resolution


def _resolution_for_issue(db: Session, issue_id: int) -> ResolutionRecord:
    issue = db.query(IssueRecord).filter_by(id=issue_id).first()
    if issue is None:
        raise KeyError("issue not found")
    return _ensure_resolution(db, issue)


def resolution_status(db: Session, shipment_id: int) -> dict[str, Any]:
    issues = (
        db.query(IssueRecord)
        .filter_by(shipment_id=shipment_id)
        .filter(IssueRecord.status != "SUPERSEDED")
        .all()
    )
    total = len(issues)
    approved = sum(1 for i in issues if i.status == "APPROVED")
    pending = sum(1 for i in issues if i.status in ("SUGGESTED", "PENDING_APPROVAL"))
    rejected = sum(1 for i in issues if i.status == "REJECTED")
    open_count = sum(1 for i in issues if i.status == "OPEN")
    if total == 0:
        status = "VERIFIED"
    elif approved == total:
        status = "RESOLVED"
    elif pending:
        status = "PENDING_APPROVAL"
    else:
        status = "NEEDS_REVIEW"
    return {
        "shipment_id": shipment_id,
        "status": status,
        "total_issues": total,
        "resolved": approved,
        "pending_approval": pending,
        "rejected": rejected,
        "open": open_count,
    }


def _update_shipment_status(db: Session, shipment_id: int) -> None:
    shipment = db.query(ShipmentRecord).filter_by(id=shipment_id).first()
    if shipment:
        shipment.status = resolution_status(db, shipment_id)["status"]


def generate_corrected_draft(db: Session, shipment_id: int) -> dict[str, Any]:
    """Write a generated BL draft text file from approved resolutions."""
    bl = latest_version(db, shipment_id, "BL")
    if bl is None:
        raise KeyError("latest BL not found")
    approved = (
        db.query(ResolutionRecord)
        .filter_by(shipment_id=shipment_id, status="APPROVED")
        .all()
    )
    if not approved:
        raise ValueError("no approved resolutions for this shipment")

    fields = dict(bl.extracted_fields or {})
    for res in approved:
        fields[res.field_name] = res.suggested_value

    out_dir = Path(__file__).resolve().parents[2] / "generated_drafts"
    out_dir.mkdir(exist_ok=True)
    base = Path(bl.filename).stem.replace(" ", "_")
    path = out_dir / f"{base}_Corrected_v{bl.version_number}.txt"
    lines = [
        "GENERATED CORRECTED BL DRAFT",
        f"Shipment ID: {shipment_id}",
        f"Source BL version: {bl.id}",
        "This draft was generated after human-approved corrections.",
        "",
    ]
    for field in COMPARED_FIELDS:
        lines.append(f"{field}: {fields.get(field, '')}")
    path.write_text("\n".join(lines), encoding="utf-8")

    for res in approved:
        res.corrected_draft_path = str(path)
    db.commit()
    return {
        "shipment_id": shipment_id,
        "source_bl_version_id": bl.id,
        "corrected_draft_path": str(path),
        "applied_resolutions": [r.id for r in approved],
    }
