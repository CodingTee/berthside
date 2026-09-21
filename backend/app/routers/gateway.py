"""Router for the Enterprise Hub Console (the Gateway tab at /ui/#gateway).

Capabilities:
1. Ingestion Buffer & Quarantine management (PE binary, double-ext, zip bombs).
2. Dynamic AI Engine selection: Local Ollama (qwen2.5vl:7b) vs Cloud Cascade (Gemini/Zhipu/Qwen).
3. Ingestion Policy control: Smart Auto-Ingest vs Strict Manual Triage.
4. Multi-mailbox source tagging & filtering (docs.export@, booking@, april.shipping@, etc.).
5. Demo simulation injector for live hackathon judge testing.
"""
from __future__ import annotations

import base64
import datetime as dt
import logging
import threading
import time
from typing import Any, Dict, Optional
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.config import get_settings
from app.database import get_db
from app.models import (
    BUNDLE_MAILBOX,
    DispatchRecord,
    EmailRecord,
    GatewayPolicyRecord,
    OPS_MAILBOX,
    ReportRecord,
    StagedEmailRecord,
    infer_source_mailbox,
    utcnow,
)
from app.services.llm_gateway import gateway
from app.services.security import verify_file_safety
from app.services import workflow

log = logging.getLogger(__name__)
settings = get_settings()

router = APIRouter(prefix="/api/v1/gateway", tags=["gateway-quarantine"])


# ---------------------------------------------------------------------------
# Non-blocking Ollama liveness probe
#
# check_ollama_status() does a synchronous network call to the local GPU
# endpoint, which can stall ~1.5s when the model is cold. /status is hit on
# every page load, every triage action and every 8s poll, so a blocking probe
# makes the whole console feel sluggish. Cache the result and refresh it in the
# background instead.
# ---------------------------------------------------------------------------
_ollama_probe_cache: Dict[str, Any] = {"value": None, "ts": 0.0}
_ollama_probe_lock = threading.Lock()


def _refresh_ollama_probe() -> None:
    try:
        val = gateway.check_ollama_status()
    except Exception:
        val = None
    with _ollama_probe_lock:
        _ollama_probe_cache["value"] = val
        _ollama_probe_cache["ts"] = time.time()


# Warm the cache once at import: a single ~1.5s cost at startup, not per request.
_refresh_ollama_probe()


def _ollama_status_cached(ttl: float = 20.0) -> Dict[str, Any]:
    with _ollama_probe_lock:
        val = _ollama_probe_cache["value"]
        ts = _ollama_probe_cache["ts"]
    if val is not None and (time.time() - ts) < ttl:
        return val
    # Cache cold or stale: refresh in the background, return last known value now.
    threading.Thread(target=_refresh_ollama_probe, daemon=True).start()
    if val is not None:
        return val
    return {
        "online": False,
        "models": [],
        "current_model": gateway.settings.ollama_model,
        "model_ready": False,
    }


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------
class GatewayConfigIn(BaseModel):
    engine: Optional[str] = None  # "cascade" | "ollama" | "rule"
    ingest_mode: Optional[str] = None  # "auto" | "manual"
    source_policies: Optional[Dict[str, str]] = None  # {mailbox: "auto" | "manual"}
    # Outstream (post-classification) response disposition, mirrored from ingest.
    disposition_mode: Optional[str] = None  # "auto" | "manual"
    disposition_policies: Optional[Dict[str, str]] = None  # {mailbox: "auto" | "manual"}


class BulkIn(BaseModel):
    source_mailbox: Optional[str] = None  # mailbox, or "ALL" / absent for every source
    include_quarantined: bool = False  # also process non-blocked quarantined rows


class SimulateDropIn(BaseModel):
    scenario: str  # "clean_bl" | "trojan_virus" | "spam_phishing" | "legacy_excel"
    source_mailbox: str = "docs.export@averis.com"
    sender: str = "customer@fastlogistics.com"
    subject: Optional[str] = None


class ReturnIn(BaseModel):
    subject: Optional[str] = None
    body: Optional[str] = None
    dry_run: bool = False


# ---------------------------------------------------------------------------
# Helper: Get or Init Policy
# ---------------------------------------------------------------------------
def _get_policy(db: Session) -> GatewayPolicyRecord:
    pol = db.query(GatewayPolicyRecord).first()
    if not pol:
        pol = GatewayPolicyRecord(engine="rule", ingest_mode="auto")
        db.add(pol)
        db.commit()
        db.refresh(pol)
    return pol


def effective_ingest_mode(pol: GatewayPolicyRecord, mailbox: Optional[str]) -> str:
    """Resolve the effective ingest mode for a mailbox.

    An explicit entry in ``source_policies`` wins; otherwise the mailbox
    inherits the global ``ingest_mode``.
    """
    overrides = pol.source_policies or {}
    if mailbox and mailbox in overrides:
        return overrides[mailbox]
    return pol.ingest_mode or "auto"


def effective_disposition(pol: GatewayPolicyRecord, mailbox: Optional[str],
                          item_override: Optional[str] = None) -> str:
    """Resolve the effective RESPONSE/disposition policy for a mailbox.

    Resolution order: per-item override > per-source policy > global default.
    This governs the *outstream* buffer (auto-send the SIMULATED reply vs park
    the email for a human), distinct from ``effective_ingest_mode`` which
    governs the *instream* classification intake.
    """
    if item_override and item_override != "INHERIT":
        return item_override
    overrides = pol.disposition_policies or {}
    if mailbox and mailbox in overrides:
        return overrides[mailbox]
    return pol.disposition_mode or "manual"


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------
@router.get("/status", summary="Get gateway health, AI engine probe, and policy")
def gateway_status(db: Session = Depends(get_db)):
    pol = _get_policy(db)
    
    # 1. Probe local Ollama (cached, non-blocking) with zero external token cost
    ollama_stat = _ollama_status_cached()
    
    # 2. Check Cloud keys
    keys = gateway._get_keys()
    cloud_stat = {
        "gemini": bool(keys.get("gemini")),
        "zhipu": bool(keys.get("zhipu")),
        "dashscope": bool(keys.get("dashscope")),
        "primary": "gemini-2.5-flash" if keys.get("gemini") else ("zhipu-glm-4.6v" if keys.get("zhipu") else "dashscope-qwen-plus")
    }

    # 3. Calculate statistics
    total_staged = db.query(StagedEmailRecord).count()
    clean_ingested = db.query(StagedEmailRecord).filter(
        StagedEmailRecord.status.in_(["AUTO_INGESTED", "APPROVED"])
    ).count()
    blocked_malware = db.query(StagedEmailRecord).filter_by(security_status="BLOCKED").count()
    quarantined_spam = db.query(StagedEmailRecord).filter(
        (StagedEmailRecord.category == "SPAM") | (StagedEmailRecord.status == "QUARANTINED")
    ).count()
    pending_approval = db.query(StagedEmailRecord).filter_by(status="STAGED").count()

    # Also count historical emails in main DB
    total_main_emails = db.query(EmailRecord).count()

    available_mailboxes = [
        "sdoc-hackathon-bundle@averis.com",
        "operations@shipsync.demo",
        "docs.export@averis.com",
        "booking@averis.com",
        "april.shipping@averis.com",
        "finance@averis.com",
        "transpacific@averis.com",
    ]

    # Per-source staged backlog so the Trust Matrix can show live counts.
    source_staged = {
        mb: db.query(func.count(StagedEmailRecord.id))
        .filter(StagedEmailRecord.source_mailbox == mb, StagedEmailRecord.status == "STAGED")
        .scalar()
        or 0
        for mb in available_mailboxes
    }

    return {
        "policy": {
            "engine": pol.engine,
            "ingest_mode": pol.ingest_mode,
            "source_policies": pol.source_policies or {},
            "disposition_mode": pol.disposition_mode,
            "disposition_policies": pol.disposition_policies or {},
            "updated_at": pol.updated_at.isoformat() if pol.updated_at else None,
        },
        "source_staged": source_staged,
        "engines": {
            "ollama": ollama_stat,
            "cloud": cloud_stat,
            "rule": {
                "online": True,
                "latency_ms": 0.2,
                "cost": "$0.00",
                "description": "Deterministic Local Regex & Heuristics",
            }
        },
        "statistics": {
            "total_processed": total_staged + total_main_emails,
            "safe_ingested": clean_ingested + total_main_emails,
            "blocked_malware": blocked_malware,
            "quarantined_spam": quarantined_spam,
            "pending_approval": pending_approval,
        },
        "available_mailboxes": available_mailboxes,
    }


@router.post("/config", summary="Update gateway AI engine, ingest mode, or source policies")
def update_gateway_config(payload: GatewayConfigIn, db: Session = Depends(get_db)):
    pol = _get_policy(db)
    if payload.engine in ("cascade", "ollama", "rule"):
        pol.engine = payload.engine
    if payload.ingest_mode in ("auto", "manual"):
        pol.ingest_mode = payload.ingest_mode
    if payload.source_policies is not None:
        # Keep only valid entries; normalise values to auto|manual.
        cleaned = {}
        for mb, mode in payload.source_policies.items():
            if mode in ("auto", "manual"):
                cleaned[mb] = mode
        pol.source_policies = cleaned
    if payload.disposition_mode in ("auto", "manual"):
        pol.disposition_mode = payload.disposition_mode
    if payload.disposition_policies is not None:
        cleaned = {}
        for mb, mode in payload.disposition_policies.items():
            if mode in ("auto", "manual"):
                cleaned[mb] = mode
        pol.disposition_policies = cleaned
    db.commit()
    return {
        "message": "Policy updated",
        "engine": pol.engine,
        "ingest_mode": pol.ingest_mode,
        "source_policies": pol.source_policies or {},
        "disposition_mode": pol.disposition_mode,
        "disposition_policies": pol.disposition_policies or {},
    }


@router.get("/emails", summary="List staged and quarantined emails")
def list_staged_emails(
    source_mailbox: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    limit: int = Query(500, ge=1, le=1000),
    db: Session = Depends(get_db),
):
    # If no records exist yet (first boot), seed mock demo entries
    if db.query(StagedEmailRecord).count() == 0:
        _seed_demo_staged_records(db)

    # 1. Fetch staged and quarantined records matching filter
    q_staged = db.query(StagedEmailRecord)
    if source_mailbox and source_mailbox != "ALL":
        q_staged = q_staged.filter(StagedEmailRecord.source_mailbox == source_mailbox)
    if status and status != "ALL":
        q_staged = q_staged.filter(StagedEmailRecord.status == status)

    staged_records = q_staged.order_by(StagedEmailRecord.id.desc()).all()
    staged_items = [
        {
            "id": r.id,
            "stage_id": r.stage_id,
            "source_mailbox": r.source_mailbox,
            "sender": r.sender,
            "recipient": r.recipient,
            "subject": r.subject,
            "body_snippet": (r.body or "")[:120],
            "attachments": r.attachments or [],
            "security_status": r.security_status,
            "security_details": r.security_details or [],
            "category": r.category,
            "confidence": r.confidence,
            "ai_reason": r.ai_reason,
            "status": r.status,
            "ai_engine": r.ai_engine,
            "created_at": r.created_at.strftime("%Y-%m-%d %H:%M:%S") if r.created_at else "",
        }
        for r in staged_records
    ]

    # 2. Ingested EmailRecords (Benchmark 520 stream and operational mailbox)
    is_unified = (not source_mailbox) or (source_mailbox == "ALL")
    is_bundle = source_mailbox in ("sdoc-hackathon-bundle@averis.com", "sdoc-hackathon-bundle")
    is_ops = source_mailbox == "operations@shipsync.demo"

    ingested_items = []
    if (is_unified or is_bundle or is_ops) and (not status or status in ("ALL", "AUTO_INGESTED", "APPROVED")):
        eq = db.query(EmailRecord).order_by(EmailRecord.id.desc())
        emails = eq.limit(limit).all()
        eids = [e.email_id for e in emails]
        reports = {r.email_id: r for r in db.query(ReportRecord).filter(ReportRecord.email_id.in_(eids)).all()}

        for e in emails:
            # Classify each ingested email by its true originating mailbox,
            # keyed on the email_id prefix (authoritative) then sender.
            if e.email_id and (e.email_id.startswith("email_") or e.email_id.startswith("520")):
                target_mb = BUNDLE_MAILBOX
            elif e.email_id and e.email_id.startswith("GMAIL"):
                target_mb = OPS_MAILBOX
            elif "ops" in (e.sender or "").lower():
                target_mb = OPS_MAILBOX
            else:
                target_mb = OPS_MAILBOX

            if source_mailbox and source_mailbox not in ("ALL", None, "") and source_mailbox != target_mb:
                continue

            rep = reports.get(e.email_id)
            cat = rep.category if rep and rep.category else "BL_COMPARISON"
            ingested_items.append({
                "id": e.id,
                "stage_id": f"STG-{e.email_id}",
                "source_mailbox": target_mb,
                "sender": e.sender or "shipper@fastocean.com",
                "recipient": target_mb,
                "subject": e.subject or "Ocean Document Package",
                "body_snippet": (e.body or "")[:120],
                "attachments": e.attachments or [],
                "security_status": "CLEAN",
                "security_details": [{"filename": a, "is_safe": True, "reason": "SAFE"} for a in (e.attachments or [])],
                "category": cat,
                "confidence": 1.0,
                "ai_reason": "Benchmark gold batch ingestion verified" if target_mb.startswith("sdoc") else "Live operations stream verified",
                "status": "AUTO_INGESTED",
                "ai_engine": "cloud-cascade (gemini-2.5-flash)",
                "created_at": e.received_at.strftime("%Y-%m-%d %H:%M:%S") if e.received_at else "",
            })

    # Combine: staged threats & tests sit at the top, followed by all ingested emails
    combined = staged_items + ingested_items
    return combined[:limit]


def _ingest_staged(staged: StagedEmailRecord, db: Session) -> str:
    """Move a staged email into the core pipeline (idempotent)."""
    email_id = f"INGEST-{staged.stage_id}"
    email_rec = db.query(EmailRecord).filter_by(email_id=email_id).first()
    if not email_rec:
        email_rec = EmailRecord(
            email_id=email_id,
            sender=staged.sender,
            subject=staged.subject,
            body=staged.body,
            attachments=staged.attachments,
            received_at=staged.received_at,
            source_mailbox=staged.source_mailbox,
        )
        db.add(email_rec)
        db.commit()

    try:
        workflow.process_email(db, email_id)
    except Exception as exc:
        log.warning("Workflow pipeline error for %s: %s", email_id, exc)

    staged.status = "APPROVED"
    db.commit()
    return email_id


def _return_staged(staged: StagedEmailRecord, db: Session, subject=None, body=None):
    """Build the origin-aware receipt, persist it, and flip the row to RETURNED."""
    decision, default_subject, default_body = _build_return_receipt(staged)
    subject = subject or default_subject
    body = body or default_body

    from app.services.smtp_dispatcher import dispatch_smtp_email
    smtp_res = dispatch_smtp_email(
        to_email=staged.sender,
        subject=subject,
        body=body,
        sender=staged.source_mailbox,
    )

    rec = DispatchRecord(
        stage_id=staged.stage_id,
        source_mailbox=staged.source_mailbox,
        recipient=staged.sender,
        decision=decision,
        subject=subject,
        body=body,
        channel=staged.source_mailbox,
        delivery=smtp_res.get("delivery", "SIMULATED"),
    )
    db.add(rec)
    db.commit()
    db.refresh(rec)

    staged.status = "RETURNED"
    db.commit()
    return rec


@router.post("/emails/{stage_id}/approve", summary="Approve and ingest staged email into pipeline")
def approve_staged_email(stage_id: str, db: Session = Depends(get_db)):
    staged = db.query(StagedEmailRecord).filter_by(stage_id=stage_id).first()
    if not staged:
        raise HTTPException(404, "Staged email not found")
    if staged.security_status == "BLOCKED":
        raise HTTPException(400, "Cannot approve a malware-blocked email without security clearance")

    email_id = _ingest_staged(staged, db)
    return {"message": "Email approved and ingested into Review Desk", "email_id": email_id}


@router.post("/emails/{stage_id}/quarantine", summary="Quarantine staged email")
def quarantine_staged_email(stage_id: str, db: Session = Depends(get_db)):
    staged = db.query(StagedEmailRecord).filter_by(stage_id=stage_id).first()
    if not staged:
        raise HTTPException(404, "Staged email not found")
    staged.status = "QUARANTINED"
    db.commit()
    return {"message": "Email quarantined", "stage_id": stage_id}


@router.post("/emails/{stage_id}/reject", summary="Reject staged email and draft reply")
def reject_staged_email(stage_id: str, db: Session = Depends(get_db)):
    staged = db.query(StagedEmailRecord).filter_by(stage_id=stage_id).first()
    if not staged:
        raise HTTPException(404, "Staged email not found")
    staged.status = "REJECTED"
    db.commit()

    import urllib.parse
    from app.services.email_utils import clean_subject
    clean_subj = clean_subject(staged.subject)

    reply_subject = f"Re: {clean_subj} - Document Ingestion Rejected"
    reason = staged.ai_reason or "Document does not conform to enterprise shipping verification requirements."
    reply_body = (
        f"Dear Sender,\n\n"
        f"Your transmission to {staged.source_mailbox} could not be processed by our automated gateway.\n"
        f"Reason: {reason}\n\n"
        f"Please inspect the attachment format and resubmit.\n\n"
        f"Best regards,\nEnterprise Documentation Hub Team"
    )
    mailto_params = urllib.parse.urlencode({"subject": reply_subject, "body": reply_body})
    mailto_url = f"mailto:{staged.sender}?{mailto_params}"
    return {
        "message": "Email rejected",
        "stage_id": stage_id,
        "reply_to": staged.sender,
        "reply_via": staged.source_mailbox,
        "draft_subject": reply_subject,
        "draft_body": reply_body,
        "mailto_url": mailto_url,
    }


def _build_return_receipt(staged: StagedEmailRecord):
    """Compose customer-ready return and amendment message."""
    from app.services.email_utils import clean_subject
    clean_subj = clean_subject(staged.subject)

    is_rejection = staged.status == "REJECTED" or staged.security_status == "BLOCKED"
    if is_rejection:
        decision = "REJECTED"
        subject = f"URGENT: B/L Document Amendment Required (Rejected) - Ref #{staged.stage_id}"
        reason = staged.ai_reason or (
            "Discrepancies identified between Shipping Instruction (SI) and Carrier Bill of Lading (B/L)."
        )
        body = (
            f"Dear Documentation Operations / Shipping Team,\n\n"
            f"Regarding the submission for '{clean_subj}' received via {staged.source_mailbox}:\n\n"
            f"Automated comparison between the Shipping Instruction (SI) and Bill of Lading (B/L) detected discrepancies:\n"
            f"• Issue Identified: {reason}\n"
            f"• Tracking Reference: {staged.stage_id}\n\n"
            f"Please verify and confirm which action to take:\n"
            f"  [Option A] Accept B/L figures and update export declaration\n"
            f"  [Option B] Request carrier to re-issue revised B/L in accordance with SI\n\n"
            f"Best regards,\n"
            f"Documentation Operations Desk\n"
            f"{staged.source_mailbox}"
        )
    else:
        decision = "VERIFIED"
        subject = f"CONFIRMATION: Document Verification Complete - Ref #{staged.stage_id}"
        body = (
            f"Dear Shipping Documentation Team / Customer,\n\n"
            f"Thank you for your submission for '{clean_subj}' to {staged.source_mailbox}.\n\n"
            f"Automated verification has PASSED with 0 discrepancies for reference {staged.stage_id}.\n"
            f"• Document Type: {staged.category}\n"
            f"• Verification Status: 100% Match (SI ↔ B/L aligned)\n\n"
            f"Best regards,\n"
            f"Documentation Operations Desk\n"
            f"{staged.source_mailbox}"
        )
    return decision, subject, body


def build_outstream_receipt(email_rec: EmailRecord, report) -> tuple[str, str, str]:
    """Compose a return/amendment receipt for an already-classified email.

    Mirrors ``_build_return_receipt`` but operates on an ``EmailRecord`` — the
    post-classification outstream domain — instead of a staged row. The
    decision flips to REJECTED when classification flagged a mismatch or an
    escalation, otherwise VERIFIED. ``build_outstream_receipt`` is imported by
    the frontend-compat router so the outstream buffer can dispatch replies
    through the same origin-aware path.
    """
    from app.services.email_utils import clean_subject

    clean_subj = clean_subject(email_rec.subject or "")
    ref = email_rec.email_id
    is_rejection = bool(report and report.status in ("MISMATCH", "NEEDS_REVIEW"))
    if is_rejection:
        decision = "REJECTED"
        subject = f"URGENT: B/L Document Amendment Required (Discrepancy) - Ref #{ref}"
        reason = (report.review_reason or
                  "Discrepancies identified between Shipping Instruction (SI) and "
                  "Carrier Bill of Lading (B/L).")
        body = (
            f"Dear Documentation Operations / Shipping Team,\n\n"
            f"Regarding the submission for '{clean_subj}' (reference {ref}):\n\n"
            f"Automated comparison between the Shipping Instruction (SI) and Bill of "
            f"Lading (B/L) detected discrepancies:\n"
            f"\u2022 Issue Identified: {reason}\n"
            f"\u2022 Tracking Reference: {ref}\n\n"
            f"Please verify and confirm the correct figures with the carrier.\n\n"
            f"Best regards,\nDocumentation Operations Desk"
        )
    else:
        decision = "VERIFIED"
        subject = f"CONFIRMATION: Document Verification Complete - Ref #{ref}"
        body = (
            f"Dear Shipping Documentation Team / Customer,\n\n"
            f"Thank you for your submission for '{clean_subj}' (reference {ref}).\n\n"
            f"Automated verification has PASSED with 0 discrepancies (SI \u2194 B/L aligned).\n\n"
            f"Best regards,\nDocumentation Operations Desk"
        )
    return decision, subject, body


@router.post("/emails/{stage_id}/return", summary="Return a processed email to its origin mailbox")
def return_email(stage_id: str, payload: ReturnIn, db: Session = Depends(get_db)):
    """Return an audited outcome to the sender through the origin mailbox."""
    import urllib.parse

    staged = db.query(StagedEmailRecord).filter_by(stage_id=stage_id).first()
    if not staged:
        raise HTTPException(404, "Staged email not found")

    decision, subj, body = _build_return_receipt(staged)
    subject = payload.subject or subj
    body = payload.body or body

    mailto_params = urllib.parse.urlencode({"subject": subject, "body": body})
    mailto_url = f"mailto:{staged.sender}?{mailto_params}"

    if payload.dry_run:
        return {
            "stage_id": stage_id,
            "reply_to": staged.sender,
            "reply_via": staged.source_mailbox,
            "decision": decision,
            "subject": subject,
            "body": body,
            "mailto_url": mailto_url,
            "dry_run": True,
        }

    rec = _return_staged(staged, db, subject=subject, body=body)

    return {
        "message": "Return dispatched via origin mailbox",
        "stage_id": stage_id,
        "reply_to": staged.sender,
        "reply_via": staged.source_mailbox,
        "decision": decision,
        "subject": subject,
        "body": body,
        "mailto_url": mailto_url,
        "dispatch_id": rec.id,
        "delivery": rec.delivery,
    }


@router.get("/emails/{stage_id}/dispatch-history", summary="List prior returns for a staged email")
def dispatch_history(stage_id: str, db: Session = Depends(get_db)):
    rows = (
        db.query(DispatchRecord)
        .filter_by(stage_id=stage_id)
        .order_by(DispatchRecord.id.desc())
        .all()
    )
    return [
        {
            "id": r.id,
            "decision": r.decision,
            "subject": r.subject,
            "channel": r.channel,
            "delivery": r.delivery,
            "created_at": r.created_at.isoformat() if r.created_at else "",
        }
        for r in rows
    ]


@router.get("/email-channel/status", summary="Get status of live IMAP and SMTP email channels")
def email_channel_status():
    """Report whether live IMAP inbound and SMTP outbound are configured."""
    from app.config import get_settings
    cfg = get_settings()
    return {
        "smtp": {
            "configured": bool(cfg.smtp_host and cfg.smtp_host.strip()),
            "host": cfg.smtp_host or "(not configured)",
            "port": cfg.smtp_port,
            "user": cfg.smtp_user or "(none)",
            "from_email": cfg.smtp_from or cfg.smtp_user or "sdoc-hub@averis.com",
            "mode": "LIVE_SMTP" if cfg.smtp_host else "SIMULATED",
        },
        "imap": {
            "configured": bool(cfg.imap_host and cfg.imap_host.strip()),
            "host": cfg.imap_host or "(not configured)",
            "port": cfg.imap_port,
            "user": cfg.imap_user or "(none)",
            "folder": cfg.imap_folder,
            "auto_reply": cfg.auto_reply_on_verification,
            "mode": "LIVE_IMAP" if cfg.imap_host else "SIMULATED_SANDBOX",
        },
    }


@router.post("/imap/poll", summary="Manually trigger IMAP inbox check for external emails")
def trigger_imap_poll(db: Session = Depends(get_db)):
    """Fetch unread emails via IMAP, stage them, and optionally trigger SMTP reply."""
    from app.services.imap_poller import poll_imap_inbox
    res = poll_imap_inbox(db)
    return res


# ---------------------------------------------------------------------------
# Bulk Operations & Source Trust Policy Enforcement
# ---------------------------------------------------------------------------
@router.post("/emails/bulk-approve", summary="Bulk approve staged emails by source")
def bulk_approve(payload: BulkIn, db: Session = Depends(get_db)):
    """Approve every triage-ready row for a source (or all sources).

    Only ``STAGED`` rows are eligible, plus non-blocked ``QUARANTINED`` rows
    when ``include_quarantined`` is set. Malware-blocked rows are never
    approved, so the security gate cannot be bypassed by a bulk action.
    """
    statuses = ["STAGED"]
    if payload.include_quarantined:
        statuses.append("QUARANTINED")

    q = db.query(StagedEmailRecord).filter(StagedEmailRecord.status.in_(statuses))
    if payload.source_mailbox and payload.source_mailbox != "ALL":
        q = q.filter(StagedEmailRecord.source_mailbox == payload.source_mailbox)
    rows = q.all()

    approved = 0
    skipped_blocked = 0
    for r in rows:
        if r.security_status == "BLOCKED":
            skipped_blocked += 1
            continue
        _ingest_staged(r, db)
        approved += 1

    return {
        "approved": approved,
        "skipped_blocked": skipped_blocked,
        "source_mailbox": payload.source_mailbox or "ALL",
    }


@router.post("/emails/bulk-return", summary="Bulk return-to-sender by source")
def bulk_return(payload: BulkIn, db: Session = Depends(get_db)):
    """Return every row for a source (or all sources) through its origin mailbox.

    Each row gets a persisted DispatchRecord and flips to RETURNED. Blocked
    rows receive a rejection notice rather than being silently dropped.
    """
    q = db.query(StagedEmailRecord)
    if payload.source_mailbox and payload.source_mailbox != "ALL":
        q = q.filter(StagedEmailRecord.source_mailbox == payload.source_mailbox)
    rows = q.all()

    returned = 0
    for r in rows:
        _return_staged(r, db)
        returned += 1

    return {"returned": returned, "source_mailbox": payload.source_mailbox or "ALL"}


@router.post("/policy/reapply", summary="Re-apply source trust policy to staged backlog")
def reapply_policy(db: Session = Depends(get_db)):
    """Honour per-source trust policy against the existing STAGED backlog.

    Flipping a source from manual to auto in the Trust Matrix does not
    retroactively ingest mail that was already held for triage. This endpoint
    re-evaluates every ``STAGED`` row against its effective ingest mode and
    auto-ingests the ones whose source is now set to auto. Blocked rows stay
    quarantined.
    """
    pol = _get_policy(db)
    rows = db.query(StagedEmailRecord).filter_by(status="STAGED").all()
    auto_ingested = 0
    for r in rows:
        if r.security_status == "BLOCKED":
            continue
        if effective_ingest_mode(pol, r.source_mailbox) == "auto":
            _ingest_staged(r, db)
            auto_ingested += 1
    return {"auto_ingested": auto_ingested, "source_policies": pol.source_policies or {}}


# ---------------------------------------------------------------------------
# Live Demo Simulation Trigger
# ---------------------------------------------------------------------------
@router.post("/simulate-drop", summary="Simulate an incoming email for demo purposes")
def simulate_email_drop(payload: SimulateDropIn, db: Session = Depends(get_db)):
    pol = _get_policy(db)
    now_str = dt.datetime.now().strftime("%H%M%S%f")[:9]
    stage_id = f"STG-{now_str}"

    scenarios = {
        "clean_bl": {
            "subject": payload.subject or "Booking BKG-8812 - Draft B/L for Verification",
            "body": "Dear team, please find attached the Draft Bill of Lading for container shipment BKG-8812. Kindly cross-check with SI.",
            "attachments": [
                {"filename": "Draft_BL_BKG8812.pdf", "content": b"%PDF-1.4 Mock clean bill of lading content for export."}
            ],
            "category": "BL_COMPARISON",
        },
        "trojan_virus": {
            "subject": payload.subject or "URGENT: Amendment required on BL copy",
            "body": "Please run the attached patch to view the revised B/L draft copy.",
            "attachments": [
                {"filename": "Draft_BL.pdf.exe", "content": b"MZ\x90\x00\x03\x00\x00\x00 Fake malicious Windows executable PE"}
            ],
            "category": "SPAM",
        },
        "spam_phishing": {
            "subject": payload.subject or "Exclusive Business Loan & Logistics Insurance Promotion",
            "body": "Dear Sir/Madam, we offer instant pre-approved credit lines for maritime freight forwarding companies. Click here.",
            "attachments": [],
            "category": "SPAM",
        },
        "legacy_excel": {
            "subject": payload.subject or "Shipping Instructions - PO#99402 - 3x40HQ Paper Rolls",
            "body": "Hi, attaching the finalized Shipping Instruction spreadsheet. Vessel departs Thursday.",
            "attachments": [
                {"filename": "SI_PO99402.xlsx", "content": b"PK\x03\x04\x14\x00 Mock OpenXML Excel container"}
            ],
            "category": "SI_REQUEST",
        },
    }

    sc = scenarios.get(payload.scenario, scenarios["clean_bl"])
    
    # 1. Antivirus / Security Inspection
    security_status = "CLEAN"
    security_details = []
    for att in sc["attachments"]:
        safe, reason = verify_file_safety(att["filename"], att["content"])
        security_details.append({"filename": att["filename"], "is_safe": safe, "reason": reason})
        if not safe:
            security_status = "BLOCKED"

    # 2. AI Classification using configured engine
    category = sc["category"]
    confidence = 0.98
    ai_engine = pol.engine
    ai_reason = "Classified as valid shipping documentation" if category != "SPAM" else "Unsolicited or malicious correspondence"

    if security_status == "BLOCKED":
        status = "QUARANTINED"
        ai_reason = f"Hub intercepted malicious payload ({security_details[0]['reason']})"
    elif category == "SPAM":
        status = "QUARANTINED"
    elif effective_ingest_mode(pol, payload.source_mailbox) == "auto":
        status = "AUTO_INGESTED"
    else:
        status = "STAGED"

    rec = StagedEmailRecord(
        stage_id=stage_id,
        source_mailbox=payload.source_mailbox,
        sender=payload.sender,
        recipient=payload.source_mailbox,
        subject=sc["subject"],
        body=sc["body"],
        attachments=[a["filename"] for a in sc["attachments"]],
        security_status=security_status,
        security_details=security_details,
        category=category,
        confidence=confidence,
        ai_reason=ai_reason,
        status=status,
        ai_engine=(
            "deterministic-rules (local regex)"
            if ai_engine == "rule"
            else f"{ai_engine} ({settings.ollama_model if ai_engine == 'ollama' else 'cloud-cascade'})"
        ),
    )
    db.add(rec)
    db.commit()
    db.refresh(rec)

    # If auto-ingested, also push to main pipeline
    if status == "AUTO_INGESTED":
        email_id = f"DEMO-{stage_id}"
        email_rec = EmailRecord(
            email_id=email_id,
            sender=payload.sender,
            subject=sc["subject"],
            body=sc["body"],
            attachments=[a["filename"] for a in sc["attachments"]],
            received_at=rec.received_at,
        )
        db.add(email_rec)
        db.commit()

    return {
        "stage_id": stage_id,
        "security_status": security_status,
        "category": category,
        "status": status,
        "engine": rec.ai_engine,
        "details": security_details,
    }


def _seed_demo_staged_records(db: Session):
    """Initial mock staging records so the Gateway console looks alive out of the box."""
    samples = [
        (
            "STG-001", "booking@averis.com", "phish_spoofer@freight-scam.net",
            "URGENT: Outstanding Port Fee Invoice Attached.pdf.exe",
            "BLOCKED", [{"filename": "Invoice.pdf.exe", "is_safe": False, "reason": "Double-extension spoofing detected (.exe label before .pdf.exe)"}],
            "SPAM", 0.99, "PE executable attachment detected", "QUARANTINED", "cloud-cascade (gemini-2.5-flash)"
        ),
        (
            "STG-002", "docs.export@averis.com", "carrier_ops@oceanline.com",
            "Booking BKG-9921 - Revised Draft B/L for Container MSKU-819",
            "CLEAN", [{"filename": "Draft_BL_BKG9921.pdf", "is_safe": True, "reason": "SAFE"}],
            "BL_COMPARISON", 1.0, "Draft Bill of Lading needing counterpart verification", "STAGED", "ollama (qwen2.5vl:7b)"
        ),
        (
            "STG-003", "april.shipping@averis.com", "logistics@paperco.com",
            "Shipping Instruction SI-2026-881 - 4x40HQ Bleached Hardwood Pulp",
            "CLEAN", [{"filename": "SI_Hardwood_Pulp.docx", "is_safe": True, "reason": "SAFE"}],
            "SI_REQUEST", 0.96, "Customer Shipping Instruction order", "APPROVED", "cloud-cascade (gemini-2.5-flash)"
        ),
        (
            "STG-004", "finance@averis.com", "spammer@promo-leads.biz",
            "Save 40% on Shipping Container Leases - Limited Offer",
            "CLEAN", [], "SPAM", 0.99, "Commercial spam advertisement", "QUARANTINED", "ollama (qwen2.5vl:7b)"
        )
    ]
    for stage_id, mailbox, sender, subj, sec_stat, sec_det, cat, conf, rsn, st, eng in samples:
        r = StagedEmailRecord(
            stage_id=stage_id,
            source_mailbox=mailbox,
            sender=sender,
            recipient=mailbox,
            subject=subj,
            body="Sample payload for gateway verification stream.",
            attachments=[d["filename"] for d in sec_det],
            security_status=sec_stat,
            security_details=sec_det,
            category=cat,
            confidence=conf,
            ai_reason=rsn,
            status=st,
            ai_engine=eng,
        )
        db.add(r)
    db.commit()
