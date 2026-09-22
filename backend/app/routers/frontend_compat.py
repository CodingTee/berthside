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
from app.models import (
    DispatchRecord,
    OutboundApprovalRecord,
    DocumentVersionRecord,
    EmailRecord,
    GatewayPolicyRecord,
    ReportRecord,
    ReviewRecord,
    ShipmentRecord,
    infer_source_mailbox,
)
from app.routers.gateway import build_outstream_receipt, effective_disposition
from app.services import inbox_service, workflow

settings = get_settings()

router = APIRouter(prefix="/api", tags=["frontend-compat"])

_CATEGORIES = ["BL_COMPARISON", "SI_REQUEST", "INVOICE_QUERY", "GENERAL", "SPAM"]


def _get_disposition_policy(db: Session) -> GatewayPolicyRecord:
    """Fetch (or lazily create) the gateway policy row for disposition lookups."""
    pol = db.query(GatewayPolicyRecord).first()
    if not pol:
        pol = GatewayPolicyRecord(
            engine="rule", ingest_mode="auto",
            disposition_mode="manual", disposition_policies={},
        )
        db.add(pol)
        db.commit()
        db.refresh(pol)
    return pol


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
    limit: int = Query(500, ge=1, le=2000),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
):
    """Inbox list with the same filters the UI sends.

    Outstream-relevant fields (source mailbox, disposition, reply state) are
    enriched from EmailRecord / GatewayPolicyRecord / DispatchRecord so the
    Outstream tab can render the post-classification disposition buffer without
    a second round-trip.
    """
    emails = inbox_service.all_emails()
    rep_map = {r.email_id: r for r in db.query(ReportRecord).all()}

    # EmailRecords carry the source mailbox + per-item disposition override.
    email_rec_map = {e.email_id: e for e in db.query(EmailRecord).all()}
    # A DispatchRecord linked by email_id means a reply was already dispatched.
    latest_dispatch = {}
    for dispatch in db.query(DispatchRecord).order_by(DispatchRecord.id.desc()).all():
        linked_id = dispatch.email_id
        if not linked_id:
            candidates = ["INGEST-" + dispatch.stage_id]
            if dispatch.stage_id.startswith("STG-"):
                candidates.append(dispatch.stage_id[4:])
            linked_id = next((key for key in candidates if key in email_rec_map), None)
        if linked_id:
            latest_dispatch.setdefault(linked_id, dispatch)
    pol = _get_disposition_policy(db)

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

        email_rec = email_rec_map.get(eid)
        mb = (email_rec.source_mailbox
              if email_rec and email_rec.source_mailbox
              else infer_source_mailbox(eid, e.get("from")))
        override = (email_rec.disposition_override
                    if email_rec and email_rec.disposition_override
                    else "INHERIT")
        eff = effective_disposition(pol, mb, override)
        dispatch = latest_dispatch.get(eid)
        has_dispatch = dispatch is not None
        rstatus = r.status if r else None
        if has_dispatch:
            delivery = (dispatch.delivery or "").upper()
            reply_state = ("sent" if delivery in {"SENT", "SENT_SMTP"}
                           else "failed" if delivery in {"FAILED", "ERROR"}
                           else "simulated" if delivery == "SIMULATED"
                           else "unknown")
        elif rstatus in ("MISMATCH", "NEEDS_REVIEW"):
            reply_state = "awaiting"
        else:
            reply_state = "none"

        items.append({
            "email_id": eid,
            "from": e.get("from") or "",
            "subject": e.get("subject") or "",
            "n_attachments": len(e.get("attachments") or []),
            "category": r.category if r else None,
            "status": rstatus,
            "has_defect": bool(r.has_defect) if r else False,
            "defect_fields": (r.defect_fields or []) if r else [],
            "review_reason": r.review_reason if r else None,
            "classify_source": (r.classify_source or None) if r else None,
            "decided_by": "human" if (r and r.reviewed) else "rule",
            "source_mailbox": mb,
            "disposition_override": override,
            "effective_disposition": eff,
            "has_dispatch": has_dispatch,
            "reply_state": reply_state,
            "delivery": dispatch.delivery if dispatch else None,
            "last_error": (dispatch.error or None) if dispatch else None,
            "last_dispatch_at": dispatch.created_at.isoformat() if dispatch and dispatch.created_at else None,
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
    dv = db.query(DocumentVersionRecord).filter_by(email_id=email_id).first()
    shipment_id = dv.shipment_id if dv else None
    shipment_key = None
    if shipment_id:
        sh = db.query(ShipmentRecord).filter_by(id=shipment_id).first()
        shipment_key = sh.shipment_key if sh else None

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
            "classify_source": report.classify_source or None,
            "has_defect": bool(report.has_defect),
            "defect_fields": report.defect_fields or [],
            "decided_by": "human" if report.reviewed else "rule",
            "rule": None,
        } if report else None,
        "comparisons": comparisons,
        "human_review": human_review,
        "shipment_id": shipment_id,
        "shipment_key": shipment_key,
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


# --------------------------------------------------- outstream disposition
class DispositionIn(BaseModel):
    override: str                       # INHERIT | AUTO | MANUAL


class OutstreamReturnIn(BaseModel):
    subject: Optional[str] = None
    body: Optional[str] = None
    dry_run: bool = False
    approval_id: Optional[str] = None


def _reply_snapshot(email_id, payload, db):
    import hashlib, json
    email = db.query(EmailRecord).filter_by(email_id=email_id).first()
    if not email:
        raise HTTPException(404, "Email not found")
    report = db.query(ReportRecord).filter_by(email_id=email_id).first()
    decision, default_subject, default_body = build_outstream_receipt(email, report)
    subject = default_subject if payload.subject is None else payload.subject
    body = default_body if payload.body is None else payload.body
    mailbox = email.source_mailbox or infer_source_mailbox(email_id, email.sender)
    recipient = email.sender or mailbox
    if not recipient or not subject.strip() or not body.strip():
        raise HTTPException(422, "Recipient, subject and body are required")
    fingerprint = hashlib.sha256(json.dumps([email_id, recipient, mailbox, subject, body, []], ensure_ascii=False).encode()).hexdigest()
    return email, decision, subject, body, mailbox, recipient, fingerprint


@router.post("/emails/{email_id}/approve-reply")
def approve_reply(email_id: str, payload: OutstreamReturnIn, db: Session = Depends(get_db)):
    from uuid import uuid4
    email, decision, subject, body, mailbox, recipient, fingerprint = _reply_snapshot(email_id, payload, db)
    # A new approval supersedes all unconsumed approvals for this message.
    db.query(OutboundApprovalRecord).filter_by(email_id=email_id, status="APPROVED").update({"status": "SUPERSEDED"})
    row = OutboundApprovalRecord(id=str(uuid4()), email_id=email_id, fingerprint=fingerprint,
        recipient=recipient, subject=subject, body=body, reviewer="Local operator (identity not authenticated)", status="APPROVED")
    db.add(row); db.commit()
    return {"approval_id": row.id, "reviewer": row.reviewer, "approved_at": row.created_at.isoformat()}


@router.get("/emails/{email_id}/reply-approvals")
def reply_approvals(email_id: str, db: Session = Depends(get_db)):
    rows = db.query(OutboundApprovalRecord).filter_by(email_id=email_id).order_by(OutboundApprovalRecord.created_at.desc()).all()
    return [{"id": x.id, "status": x.status, "reviewer": x.reviewer, "created_at": x.created_at.isoformat(),
             "recipient": x.recipient, "subject": x.subject, "body": x.body, "dispatch_id": x.dispatch_id} for x in rows]


@router.post("/emails/{email_id}/disposition")
def set_disposition(email_id: str, payload: DispositionIn,
                    db: Session = Depends(get_db)):
    """Set the per-item disposition override for an email.

    INHERIT falls back to the per-source / global disposition policy; AUTO or
    MANUAL pins it. Resolution is ``effective_disposition`` in the gateway
    router, so this is the single-item lever of the outstream buffer.
    """
    if payload.override not in ("INHERIT", "AUTO", "MANUAL"):
        raise HTTPException(400, "override must be one of INHERIT | AUTO | MANUAL")
    email_rec = db.query(EmailRecord).filter_by(email_id=email_id).first()
    if not email_rec:
        raise HTTPException(404, f"email not found: {email_id}")
    email_rec.disposition_override = payload.override
    db.commit()
    pol = _get_disposition_policy(db)
    mb = email_rec.source_mailbox or infer_source_mailbox(email_id, email_rec.sender)
    eff = effective_disposition(pol, mb, email_rec.disposition_override)
    return {
        "email_id": email_id,
        "disposition_override": email_rec.disposition_override,
        "effective_disposition": eff,
    }


@router.post("/emails/{email_id}/return")
def outstream_return(email_id: str, payload: OutstreamReturnIn,
                     db: Session = Depends(get_db)):
    """Dispatch the post-classification reply for an ingested email.

    Reuses the gateway's origin-aware receipt builder and persists a
    DispatchRecord (delivery=SIMULATED unless a live SMTP host is configured).
    This is the outstream analogue of the gateway's return-to-sender: the email
    is already classified, so the disposition decides whether the reply goes
    out automatically or waits for a human.
    """
    email_rec, decision, subject, body, mb, recipient, fingerprint = _reply_snapshot(email_id, payload, db)
    mode = effective_disposition(_get_disposition_policy(db), mb, email_rec.disposition_override or "INHERIT")
    if payload.dry_run:
        return {"email_id": email_id, "reply_via": mb, "recipient": recipient, "decision": decision,
                "subject": subject, "body": body, "dry_run": True, "effective_disposition": mode,
                "attachments": [], "approval_required": mode != "auto"}
    approval = None
    if payload.approval_id:
        approval = db.query(OutboundApprovalRecord).filter_by(id=payload.approval_id, email_id=email_id).first()
        if not approval or approval.fingerprint != fingerprint or approval.status != "APPROVED":
            raise HTTPException(409, "Approval is stale, already used, or does not match this reply. Review again.")
        from datetime import datetime, timedelta, timezone
        if datetime.now(timezone.utc) - approval.created_at.replace(tzinfo=timezone.utc) > timedelta(hours=24):
            raise HTTPException(409, "Approval expired. Review again.")
        claimed = db.query(OutboundApprovalRecord).filter_by(id=approval.id, status="APPROVED").update({"status": "SENDING"})
        if claimed != 1:
            db.rollback()
            raise HTTPException(409, "This approval is already being sent")
        db.commit()  # Claim before SMTP: repeated requests cannot reuse approval.
    elif mode != "auto":
        raise HTTPException(409, "Manual policy requires approval of the exact reply before sending")

    from app.services.smtp_dispatcher import dispatch_smtp_email
    try:
        smtp_res = dispatch_smtp_email(to_email=recipient, subject=subject, body=body, sender=mb)
    except Exception:
        if approval:
            approval.status = "UNCONFIRMED"
            db.commit()
        raise HTTPException(502, "Delivery outcome is unconfirmed. Check sending history before retrying.")
    rec = DispatchRecord(
        email_id=email_id,
        stage_id=f"OUT-{email_id}",
        source_mailbox=mb,
        recipient=email_rec.sender or mb,
        decision=decision,
        subject=subject,
        body=body,
        channel=mb,
        delivery="FAILED" if smtp_res.get("status") == "ERROR" else smtp_res.get("delivery", "SIMULATED"),
        gmail_message_id=smtp_res.get("message_id"),
        error=(smtp_res.get("error") or "")[:512] or None,
    )
    db.add(rec)
    db.commit()
    db.refresh(rec)
    if approval:
        approval.status = rec.delivery
        approval.dispatch_id = rec.id
        db.commit()
    return {
        "email_id": email_id, "reply_via": mb, "decision": decision,
        "subject": subject, "body": body,
        "dispatch_id": rec.id, "delivery": rec.delivery,
        "error": smtp_res.get("error"),
    }


@router.get("/emails/{email_id}/dispatch-history")
def outstream_dispatch_history(email_id: str, db: Session = Depends(get_db)):
    """List prior outstream replies dispatched for an email."""
    from sqlalchemy import and_, or_
    stage_ids = [f"STG-{email_id}"]
    if email_id.startswith("INGEST-"):
        stage_ids.append(email_id[len("INGEST-"):])
    rows = (
        db.query(DispatchRecord)
        .filter(or_(DispatchRecord.email_id == email_id,
                    and_(DispatchRecord.email_id.is_(None),
                         DispatchRecord.stage_id.in_(stage_ids))))
        .order_by(DispatchRecord.id.desc())
        .all()
    )
    return [
        {
            "id": r.id,
            "decision": r.decision,
            "subject": r.subject,
            "channel": r.channel,
            "recipient": r.recipient,
            "body": r.body,
            "message_id": r.gmail_message_id,
            "delivery": r.delivery,
            "error": r.error,
            "created_at": r.created_at.isoformat() if r.created_at else "",
        }
        for r in rows
    ]


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
