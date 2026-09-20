"""Real Gmail integration endpoints."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, Response
from pathlib import Path
from sqlalchemy.orm import Session

from app.config import get_settings
from app.database import get_db
from app.integrations.gmail import auth, sync
from app.models import EmailRecord, GmailMessageRecord, ReportRecord

router = APIRouter(prefix="/api/gmail", tags=["gmail-integration"])


@router.get("/status", summary="Check Gmail integration configuration")
def gmail_status():
    return auth.integration_status()


@router.post("/credentials", summary="Save Google OAuth client credentials JSON")
async def save_gmail_credentials(request: Request):
    import json

    settings = get_settings()
    content_type = request.headers.get("content-type", "")
    client_data = None
    try:
        if "application/json" in content_type:
            body = await request.json()
            if isinstance(body, dict) and "client_json" in body:
                raw = body["client_json"]
                client_data = json.loads(raw) if isinstance(raw, str) else raw
            elif isinstance(body, dict) and ("web" in body or "installed" in body):
                client_data = body
            else:
                client_data = body
        else:
            text = (await request.body()).decode("utf-8")
            client_data = json.loads(text)
    except Exception as exc:
        raise HTTPException(400, f"Invalid JSON payload: {exc}") from exc

    if not isinstance(client_data, dict) or not ("web" in client_data or "installed" in client_data):
        raise HTTPException(
            400,
            "Invalid Google OAuth client JSON format. Expected JSON containing a 'web' or 'installed' object with client_id and client_secret.",
        )

    cred_path = Path(settings.gmail_credentials_file)
    cred_path.parent.mkdir(parents=True, exist_ok=True)
    cred_path.write_text(json.dumps(client_data, indent=2), encoding="utf-8")
    return {
        "status": "configured",
        "path": str(cred_path),
        "account": settings.gmail_demo_account,
    }


@router.get("/connect", summary="Create Google OAuth authorization URL")
def gmail_connect():
    try:
        return auth.build_authorization_url()
    except FileNotFoundError as exc:
        raise HTTPException(409, str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(501, str(exc)) from exc


@router.get("/oauth-callback", response_class=HTMLResponse,
            summary="OAuth callback for Google Gmail authorization")
def gmail_oauth_callback(code: str | None = None, error: str | None = None):
    def page(title: str, body: str, ok: bool = False) -> HTMLResponse:
        color = "#22c55e" if ok else "#f87171"
        return HTMLResponse(f"""<!doctype html><meta charset="utf-8">
<title>{title}</title>
<body style="font:15px/1.6 system-ui;background:#0b1020;color:#e5e7eb;
             display:flex;align-items:center;justify-content:center;height:100vh;margin:0">
  <div style="max-width:560px;background:#131a2e;border:1px solid #2a3555;
              border-radius:14px;padding:28px 32px">
    <h1 style="margin:0 0 10px;font-size:19px;color:{color}">{title}</h1>
    <div style="color:#c7cfe3">{body}</div>
    <p style="margin:18px 0 0"><a href="/shipmail/" style="color:#7dd3fc">
      Back to ShipMail</a></p>
  </div></body>""")

    if error:
        return page("Gmail authorization failed",
                    f"Google returned: <code>{error}</code>.<br>"
                    "Make sure your own address is listed under "
                    "<b>OAuth consent screen → Test users</b>.", False)
    if not code:
        return page("Gmail authorization incomplete",
                    "No authorization code was returned.", False)
    try:
        result = auth.exchange_code_for_token(code)
    except FileNotFoundError as exc:
        return page("Gmail not configured", f"{exc}", False)
    except RuntimeError as exc:
        return page("Gmail token exchange failed",
                    f"{exc}<br><br>Start over: open ShipMail → <b>Connect Gmail</b>.",
                    False)
    return page("ShipMail connected to Gmail",
                f"Account: <b>{result['account']}</b><br>"
                "Close this tab, then click <b>Sync now</b> in ShipMail.", True)


@router.post("/poll", summary="Poll Gmail inbox and process unseen messages")
def poll_gmail(
    limit: int = Query(10, ge=1, le=50),
    force: bool = Query(False, description="Re-process already synced messages too"),
    db: Session = Depends(get_db),
):
    if force:
        # Explicit reprocess (same contract as /api/process force:true): reset
        # the per-message status so poll picks every stored message up again,
        # e.g. after the processing logic gained a capability.
        db.query(GmailMessageRecord).filter(
            GmailMessageRecord.processing_status == "PROCESSED"
        ).update({"processing_status": "PENDING"})
        db.commit()
    try:
        return sync.poll_and_process(db, limit=limit)
    except FileNotFoundError as exc:
        raise HTTPException(409, str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(501, str(exc)) from exc


@router.get("/messages", summary="List synced real Gmail messages")
def gmail_messages(
    limit: int = Query(50, ge=1, le=200),
    db: Session = Depends(get_db),
):
    """Read-only list of Gmail messages already pulled + processed by the
    sync job. The ShipMail UI uses this to show a *real* inbox alongside the
    simulated one. No polling, no reprocessing — just what is in the DB."""
    rows = (
        db.query(GmailMessageRecord)
        .order_by(GmailMessageRecord.created_at.desc())
        .limit(limit)
        .all()
    )
    items = []
    for rec in rows:
        email = db.query(EmailRecord).filter_by(email_id=rec.email_id).first()
        report = db.query(ReportRecord).filter_by(email_id=rec.email_id).first()
        atts = []
        if email and email.attachments:
            for p in email.attachments:
                atts.append((p or "").rsplit("/", 1)[-1].rsplit("\\", 1)[-1])
        # shipment_key is the human code, stored as "REF:SHP-001"; the raw
        # extracted.shipment_id is an internal ShipmentRecord row id that
        # would read as a confusing "91".
        ex = (report.extracted or {}) if report else {}
        ship_key = str(ex.get("shipment_key") or "")
        shipment_display = (ship_key.split(":")[-1] if ship_key
                            else ex.get("shipment_id"))
        items.append({
            "email_id": rec.email_id,
            "gmail_message_id": rec.gmail_message_id,
            "sender": rec.sender or (email.sender if email else None),
            "subject": rec.subject or (email.subject if email else None),
            "body": email.body if email else None,
            "snippet": (email.body[:140] if email and email.body else (rec.subject or "")),
            "thread_id": rec.thread_id,
            "processing_status": rec.processing_status,
            "status": report.status if report else None,
            "category": report.category if report else None,
            "has_defect": bool(report.has_defect) if report else None,
            "defect_fields": report.defect_fields or [] if report else [],
            "shipment_id": shipment_display,
            "processed_at": rec.processed_at.isoformat() if rec.processed_at else None,
            "has_attachments": bool(atts),
            "attachments": atts,
        })
    return {"total": len(items), "items": items}


@router.get("/attachment/{email_id}/{filename:path}",
            summary="Read a synced Gmail attachment by filename")
def gmail_attachment(email_id: str, filename: str, db: Session = Depends(get_db)):
    """Serve one attachment that the sync job saved under
    ``<ingest_dir>/gmail/<email_id>/``. Filename match is case-insensitive
    because attachments are sanitized to lowercase on save."""
    from app.routers.ingest import _safe_segment

    settings = get_settings()
    base = Path(settings.ingest_dir) / "gmail" / _safe_segment(email_id, "gmail")
    target = None
    if base.is_dir():
        for f in base.iterdir():
            if f.name.lower() == filename.lower():
                target = f
                break
    if target is None or not target.is_file():
        raise HTTPException(404, "Gmail attachment not found")
    if target.suffix.lower() in {".txt", ".csv", ".md", ".json", ".xml", ".log"}:
        return Response(target.read_text(encoding="utf-8", errors="replace"),
                        media_type="text/plain")
    return Response(target.read_bytes(), media_type="application/octet-stream")
