"""Microsoft Outlook integration API endpoints."""
from __future__ import annotations

from pathlib import Path
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse, HTMLResponse
from sqlalchemy.orm import Session

from app.config import get_settings
from app.database import get_oauth_db
from app.integrations.outlook import auth, sync
from app.models import EmailRecord, ReportRecord
from app.routers.ingest import _safe_segment

router = APIRouter(prefix="/api/outlook", tags=["outlook-integration"])

RESULT_PAGE_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>{title}</title>
  <style>
    body {{
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
      display: flex; align-items: center; justify-content: center;
      height: 100vh; margin: 0; background: #0f172a; color: #f8fafc; text-align: center;
    }}
    .card {{
      background: #1e293b; padding: 36px 48px; border-radius: 16px;
      box-shadow: 0 10px 25px rgba(0,0,0,0.5); max-width: 420px; border: 1px solid #334155;
    }}
    .icon {{ font-size: 48px; margin-bottom: 16px; }}
    h1 {{ font-size: 20px; margin: 0 0 12px; color: {tone}; }}
    p {{ font-size: 14px; line-height: 1.5; color: #94a3b8; margin: 0 0 20px; }}
    .btn {{
      background: #2563eb; color: #fff; border: none; padding: 10px 20px;
      border-radius: 8px; cursor: pointer; font-weight: 500; font-size: 14px;
    }}
  </style>
</head>
<body>
  <div class="card">
    <div class="icon">{icon}</div>
    <h1>{title}</h1>
    <p>{body}</p>
    <button class="btn" onclick="window.close()">Close this window</button>
  </div>
  <script>
    try {{
      localStorage.setItem('sdoc-outlook-connected', Date.now().toString());
      if (window.opener) {{
        window.opener.postMessage({{ type: 'outlook-connected' }}, '*');
      }}
    }} catch(e) {{}}
    setTimeout(() => window.close(), 2000);
  </script>
</body>
</html>"""


from pydantic import BaseModel

class OutlookConfigPayload(BaseModel):
    client_id: str
    client_secret: str | None = None
    redirect_uri: str | None = None


@router.get("/status", summary="Check Outlook integration status")
def outlook_status():
    return auth.integration_status()


@router.post("/config", summary="Set Microsoft Application (Client) ID")
def outlook_config(payload: OutlookConfigPayload):
    auth.save_client_config(
        client_id=payload.client_id,
        client_secret=payload.client_secret,
        redirect_uri=payload.redirect_uri,
    )
    return auth.integration_status()


@router.get("/connect", summary="Get Microsoft OAuth authorization URL")
def outlook_connect():
    try:
        return auth.build_authorization_url()
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/oauth-callback", response_class=HTMLResponse, summary="OAuth callback for Microsoft Outlook")
def outlook_oauth_callback(
    code: str | None = None,
    error: str | None = None,
    error_description: str | None = None,
    state: str | None = None,
):
    if error:
        err_msg = error_description or error
        return HTMLResponse(
            RESULT_PAGE_HTML.format(
                title="Outlook Connection Failed",
                body=f"Microsoft returned: <code>{err_msg}</code>",
                tone="#ef4444",
                icon="❌",
            ),
            status_code=400,
        )

    if not code:
        return HTMLResponse(
            RESULT_PAGE_HTML.format(
                title="Authorization Incomplete",
                body="No authorization code was returned by Microsoft.",
                tone="#f59e0b",
                icon="⚠️",
            ),
            status_code=400,
        )

    try:
        res = auth.exchange_code_for_token(code=code, state=state)
        account = res.get("account", "Outlook")
        return HTMLResponse(
            RESULT_PAGE_HTML.format(
                title="ShipMail Connected to Outlook!",
                body=f"Successfully linked account: <b>{account}</b><br>You can close this tab now.",
                tone="#10b981",
                icon="✅",
            )
        )
    except Exception as exc:
        return HTMLResponse(
            RESULT_PAGE_HTML.format(
                title="Token Exchange Failed",
                body=f"Could not complete handshake: {exc}",
                tone="#ef4444",
                icon="❌",
            ),
            status_code=500,
        )


@router.post("/poll", summary="Sync Outlook messages through Microsoft Graph API")
def outlook_poll(
    limit: int = Query(15, ge=1, le=50),
    db: Session = Depends(get_oauth_db),
):
    try:
        return sync.poll_and_process(db, limit=limit)
    except Exception as exc:
        raise HTTPException(500, f"Outlook sync failed: {exc}") from exc


@router.get("/messages", summary="List synced Outlook messages")
def outlook_messages(
    limit: int = Query(50, ge=1, le=200),
    db: Session = Depends(get_oauth_db),
):
    records = (
        db.query(EmailRecord)
        .filter(EmailRecord.email_id.like("OUTLOOK-%"))
        .order_by(EmailRecord.received_at.desc())
        .limit(limit)
        .all()
    )
    items = []
    for email in records:
        report = db.query(ReportRecord).filter_by(email_id=email.email_id).first()
        atts = [(p or "").rsplit("/", 1)[-1].rsplit("\\", 1)[-1] for p in (email.attachments or [])]
        ex = (report.extracted or {}) if report else {}
        ship_key = str(ex.get("shipment_key") or "")
        shipment_display = (ship_key.split(":")[-1] if ship_key else ex.get("shipment_id"))
        tok = auth.get_stored_token()
        recip = tok.get("account") if tok else "ship.demo@outlook.com"
        items.append({
            "email_id": email.email_id,
            "provider": "outlook",
            "to": recip,
            "sender": email.sender,
            "subject": email.subject,
            "body": email.body,
            "snippet": (email.body or "")[:140],
            "received_at": email.received_at.isoformat() if email.received_at else None,
            "processed_at": email.received_at.isoformat() if email.received_at else None,
            "category": report.category if report else None,
            "status": report.status if report else None,
            "shipment_id": shipment_display,
            "attachments": atts,
            "attachment_paths": list(email.attachments or []),
        })
    return {"total": len(items), "items": items}


@router.get("/attachment/{email_id}/{filename:path}", summary="Download synced Outlook attachment")
def outlook_attachment(email_id: str, filename: str):
    settings = get_settings()
    base = Path(settings.ingest_dir) / "outlook" / _safe_segment(email_id, "outlook")
    target = None
    if base.is_dir():
        for f in base.iterdir():
            if f.name.lower() == filename.lower():
                target = f
                break
    if not target or not target.is_file():
        raise HTTPException(404, f"Attachment {filename} not found for {email_id}")
    return FileResponse(target, filename=target.name)
