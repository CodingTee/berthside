"""Shipment-centred view over the existing versioning tables.

The database already stores shipments, documents and versions; what was missing
was the *shipment-level* reading of that data: is the set complete, which
document is missing, what is the current version, which emails brought it in,
and what should the operator do next.

This module answers those questions **from stored results only**. It never runs
classification, extraction or verification, so opening a shipment view costs a
database read and nothing else.

Design notes
------------
* "Missing BL" is a *shipment issue*, never a document type. No fake BL row is
  created to make a shipment look complete.
* Document completeness is derived from the documents that actually exist.
  ``INVOICE`` is expected but not required; ``SI``/``BL`` are required because
  they are the two sides of the comparison.
* The status vocabulary is the shipment one: PROCESSING / INCOMPLETE /
  NEEDS_ATTENTION / VERIFIED. The older per-issue status from
  ``versioning.resolution_status`` stays available for the resolution flow.
"""
from __future__ import annotations

from typing import Any, Optional

from sqlalchemy.orm import Session

from app.models import (
    DocumentVersionRecord,
    EmailRecord,
    IssueRecord,
    ReportRecord,
    ShipmentRecord,
)

# Required to run a comparison; INVOICE enriches but never blocks a verdict.
REQUIRED_DOC_TYPES = ("SI", "BL")
OPTIONAL_DOC_TYPES = ("INVOICE",)
ALL_DOC_TYPES = REQUIRED_DOC_TYPES + OPTIONAL_DOC_TYPES

DOC_LABELS = {
    "SI": "Shipping Instruction",
    "BL": "Bill of Lading",
    "INVOICE": "Commercial Invoice",
}

# Status vocabulary for the shipment-centred dashboard.
PROCESSING = "PROCESSING"
INCOMPLETE = "INCOMPLETE"
NEEDS_ATTENTION = "NEEDS_ATTENTION"
VERIFIED = "VERIFIED"


def _version_row(row: DocumentVersionRecord) -> dict[str, Any]:
    return {
        "id": row.id,
        "doc_type": row.doc_type,
        "version_number": row.version_number,
        "filename": row.filename,
        "email_id": row.email_id,
        "document_status": row.document_status,
        "is_latest": bool(row.is_latest),
        "is_duplicate": bool(row.duplicate_of_version_id),
        "received_at": row.received_at.isoformat() if row.received_at else None,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "extracted_fields": row.extracted_fields or {},
    }


def _documents(db: Session, shipment_id: int) -> dict[str, dict[str, Any]]:
    """Group every stored version by document type, newest version first."""
    grouped: dict[str, dict[str, Any]] = {t: _empty_doc(t) for t in ALL_DOC_TYPES}
    rows = (
        db.query(DocumentVersionRecord)
        .filter_by(shipment_id=shipment_id)
        .order_by(DocumentVersionRecord.doc_type,
                  DocumentVersionRecord.version_number.desc(),
                  DocumentVersionRecord.id.desc())
        .all()
    )
    for row in rows:
        doc_type = (row.doc_type or "").upper()
        bucket = grouped.setdefault(doc_type, _empty_doc(doc_type))
        bucket["versions"].append(_version_row(row))

    for doc_type, bucket in grouped.items():
        active = [v for v in bucket["versions"] if v["document_status"] == "ACTIVE"]
        bucket["version_count"] = len(active)
        bucket["present"] = bool(active)
        current = next((v for v in bucket["versions"] if v["is_latest"]), None)
        bucket["current_version"] = current
        bucket["current_version_number"] = (
            current["version_number"] if current else None)
    return grouped


def _empty_doc(doc_type: str) -> dict[str, Any]:
    return {
        "doc_type": doc_type,
        "label": DOC_LABELS.get(doc_type, doc_type),
        "present": False,
        "version_count": 0,
        "current_version": None,
        "current_version_number": None,
        "versions": [],
    }


def _source_emails(db: Session, shipment_id: int,
                   versions: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """Email metadata behind every stored document version."""
    email_ids: list[str] = []
    for bucket in versions.values():
        for version in bucket["versions"]:
            if version["email_id"] and version["email_id"] not in email_ids:
                email_ids.append(version["email_id"])
    if not email_ids:
        return []

    records = {
        r.email_id: r
        for r in db.query(EmailRecord).filter(EmailRecord.email_id.in_(email_ids)).all()
    }
    out: list[dict[str, Any]] = []
    for email_id in email_ids:
        rec = records.get(email_id)
        contributed = [
            v["filename"]
            for bucket in versions.values()
            for v in bucket["versions"]
            if v["email_id"] == email_id
        ]
        out.append({
            "email_id": email_id,
            "sender": rec.sender if rec else None,
            "subject": rec.subject if rec else None,
            "received_at": rec.received_at.isoformat() if rec and rec.received_at else None,
            "attachments": contributed,
            "document_types": sorted({
                doc_type for doc_type, bucket in versions.items()
                for v in bucket["versions"] if v["email_id"] == email_id
            }),
        })
    return out


def _open_issues(db: Session, shipment_id: int) -> list[IssueRecord]:
    return (
        db.query(IssueRecord)
        .filter_by(shipment_id=shipment_id)
        .filter(IssueRecord.status.notin_(["SUPERSEDED"]))
        .order_by(IssueRecord.id)
        .all()
    )


def _latest_report(db: Session, shipment_id: int,
                   versions: dict[str, dict[str, Any]]) -> Optional[ReportRecord]:
    email_ids = [
        v["email_id"]
        for bucket in versions.values()
        for v in bucket["versions"] if v["email_id"]
    ]
    if not email_ids:
        return None
    reports = (
        db.query(ReportRecord)
        .filter(ReportRecord.email_id.in_(email_ids))
        .order_by(ReportRecord.id.desc())
        .all()
    )
    if not reports:
        return None
    # The verdict that describes this shipment is the SI/BL comparison. A later
    # email on the same shipment can be unrelated to it (a revised invoice is
    # SKIPPED), and letting that one win would report the shipment as unverified
    # when the comparison itself passed.
    return next((r for r in reports if r.category == "BL_COMPARISON"), reports[0])


def build_overview(db: Session, shipment: ShipmentRecord) -> dict[str, Any]:
    """Assemble everything a shipment-centred view needs, from stored data."""
    documents = _documents(db, shipment.id)
    missing = [t for t in REQUIRED_DOC_TYPES if not documents[t]["present"]]
    missing_optional = [t for t in OPTIONAL_DOC_TYPES if not documents[t]["present"]]
    issues = _open_issues(db, shipment.id)
    report = _latest_report(db, shipment.id, documents)

    mismatch_fields = sorted({i.field_name for i in issues})
    reasons: list[str] = []
    for doc_type in missing:
        reasons.append(f"Missing {DOC_LABELS.get(doc_type, doc_type)}")
    if mismatch_fields:
        reasons.append("Field mismatch: " + ", ".join(mismatch_fields))
    if report is not None and report.status == "NEEDS_REVIEW" and report.review_reason:
        reasons.append(f"Needs review: {report.review_reason}")

    has_any_document = any(documents[t]["present"] for t in ALL_DOC_TYPES)
    if not has_any_document:
        status = PROCESSING
    elif "SI" in missing:
        # Without an SI there is nothing to compare against, so the shipment
        # cannot be verified yet — it is simply incomplete.
        status = INCOMPLETE
    elif missing or mismatch_fields or (
            report is not None and report.status == "NEEDS_REVIEW"):
        status = NEEDS_ATTENTION
    else:
        status = VERIFIED

    actions: list[str] = []
    if "BL" in missing:
        actions.append("REQUEST_BL")
    if "SI" in missing:
        actions.append("REQUEST_SI")
    if mismatch_fields:
        actions.append("REQUEST_CORRECTION")
    if report is not None and report.status == "NEEDS_REVIEW":
        actions.append("MANUAL_REVIEW")
    if status == VERIFIED:
        actions.append("NO_ACTION")

    return {
        "id": shipment.id,
        "shipment_key": shipment.shipment_key,
        # Human-facing code: the key is stored as "REF:SHP-001".
        "shipment_id": (shipment.reference_number
                        or shipment.shipment_key.split(":", 1)[-1]),
        "shipment_code": (shipment.reference_number
                          or shipment.shipment_key.split(":", 1)[-1]),
        "reference_number": shipment.reference_number,
        "status": status,
        "reasons": reasons,
        "document_types": [t for t in ALL_DOC_TYPES if documents[t]["present"]],
        "documents": documents,
        "missing_documents": missing,
        "missing_optional_documents": missing_optional,
        "complete": not missing,
        "mismatch_fields": mismatch_fields,
        "issue_count": len(issues),
        "issues": [
            {
                "id": i.id,
                "field_name": i.field_name,
                "si_value": i.si_value,
                "bl_value": i.bl_value,
                "difference": i.difference,
                "explanation": i.explanation,
                "status": i.status,
            }
            for i in issues
        ],
        "verification": {
            "status": report.status if report else None,
            "category": report.category if report else None,
            "has_defect": bool(report.has_defect) if report else False,
            "defect_fields": report.defect_fields or [] if report else [],
            "review_reason": report.review_reason if report else None,
            "field_results": report.field_results or [] if report else [],
        },
        "source_emails": _source_emails(db, shipment.id, documents),
        "actions": actions,
        "updated_at": shipment.updated_at.isoformat() if shipment.updated_at else None,
    }
