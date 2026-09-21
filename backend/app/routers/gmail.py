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


# Design tokens copied from web/index.html's :root block, so backend-generated
# pages (the OAuth callback, 404s) share the Dashboard/ShipMail look — same
# colors, font stack, radii and gradient accent — instead of a bare error page.
# Both themes ship; the page follows the shared `sdoc-theme` key via the small
# script at the bottom of each page.
RESULT_PAGE_CSS = """
:root{
  --bg:#080d18; --surface-2:#0f1829; --surface-3:#152036;
  --border:rgba(148,163,184,.17); --border-strong:rgba(148,163,184,.30);
  --text:#f2f6ff; --muted:#a3b2c9; --muted-2:#7f8ea8;
  --accent:#2dd4bf;
  --grad:linear-gradient(135deg,#2dd4bf 0%,#38bdf8 52%,#6366f1 100%);
  --ok:#34d399; --warn:#fbbf24; --bad:#fb7185;
  --shadow:0 18px 50px -20px rgba(0,0,0,.65);
  --radius:14px;
  --sans:"Inter","Segoe UI",system-ui,-apple-system,"Helvetica Neue",Arial,"PingFang SC","Microsoft YaHei",sans-serif;
  --mono:"JetBrains Mono","SFMono-Regular",Consolas,"Liberation Mono",Menlo,monospace;
}
[data-theme="light"]{
  --bg:#eef2f8; --surface-2:#ffffff; --surface-3:#f5f8fc;
  --border:rgba(15,23,42,.10); --border-strong:rgba(15,23,42,.18);
  --text:#0f1b2d; --muted:#54657f; --muted-2:#5e6d82;
  --accent:#0d9488;
  --grad:linear-gradient(135deg,#0d9488 0%,#0284c7 52%,#4f46e5 100%);
  --ok:#059669; --warn:#d97706; --bad:#e11d48;
  --shadow:0 18px 45px -22px rgba(15,23,42,.30);
}
html{background:var(--bg)}
body{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;
     background:var(--bg);color:var(--text);font:15px/1.65 var(--sans)}
.card{position:relative;overflow:hidden;max-width:560px;margin:24px;width:100%;
     background:var(--surface-2);border:1px solid var(--border-strong);
     border-radius:var(--radius);box-shadow:var(--shadow);padding:30px 34px 28px}
.card::before{content:"";position:absolute;inset:0 0 auto 0;height:3px;background:var(--grad)}
.brand{display:flex;align-items:center;gap:10px;margin-bottom:16px;
     font-size:11.5px;font-weight:700;letter-spacing:.14em;text-transform:uppercase;
     color:var(--muted-2)}
.brand .mark{width:24px;height:24px;border-radius:7px;background:var(--grad);
     display:inline-grid;place-items:center;color:#fff;flex:0 0 auto}
.brand .mark svg{width:14px;height:14px}
h1{margin:0 0 10px;font-size:20px;line-height:1.35}
.body{color:var(--muted)}
.body b{color:var(--text)}
.body code{font-family:var(--mono);font-size:12.5px;color:var(--accent);
     background:var(--surface-3);border:1px solid var(--border);
     border-radius:6px;padding:1px 6px;overflow-wrap:anywhere}
.back{display:inline-flex;align-items:center;gap:8px;margin-top:20px;padding:9px 16px;
     border-radius:10px;background:var(--surface-3);border:1px solid var(--border-strong);
     color:var(--accent);text-decoration:none;font-weight:600;font-size:13px;
     transition:filter .15s}
.back:hover{filter:brightness(1.12)}
.back svg{width:14px;height:14px}
.back-row{display:flex;gap:10px;margin-top:20px;flex-wrap:wrap}
"""

RESULT_PAGE_THEME_JS = (
    "<script>try{var t=localStorage.getItem(\"sdoc-theme\")||"
    "localStorage.getItem(\"shipmail-theme\");"
    "if(t===\"light\"||t===\"dark\")"
    "document.documentElement.setAttribute(\"data-theme\",t);}catch(_){}</script>"
)

RESULT_PAGE_MARK = (
    "<div class=\"brand\"><span class=\"mark\"><svg viewBox=\"0 0 24 24\" "
    "fill=\"none\" stroke=\"currentColor\" stroke-width=\"2.2\" "
    "stroke-linecap=\"round\" stroke-linejoin=\"round\">"
    "<path d=\"M3 8l9-5 9 5v8l-9 5-9-5z\"/><path d=\"M3 8l9 5 9-5M12 13v8\"/>"
    "</svg></span>ShipSync</div>"
)

RESULT_PAGE_BACK = (
    "<a class=\"back\" href=\"/shipmail/\">"
    "<svg viewBox=\"0 0 24 24\" fill=\"none\" stroke=\"currentColor\" "
    "stroke-width=\"2\" stroke-linecap=\"round\" stroke-linejoin=\"round\">"
    "<path d=\"M9 14L4 9l5-5\"/><path d=\"M4 9h10a6 6 0 0 1 0 12h-3\"/></svg>"
    "Back to ShipMail</a>"
)


def result_page(title: str, body: str, ok: bool = False) -> HTMLResponse:
    """A themed result page using the product's own design tokens.

    `ok` picks the status color (green success / red failure); the rest of the
    look is identical to the Dashboard so the OAuth callback never feels like a
    foreign page. Shared with main.py's 404 handler via RESULT_PAGE_CSS.
    """
    tone = "var(--ok)" if ok else "var(--bad)"
    return HTMLResponse(f"""<!doctype html>
<html lang="en" data-theme="dark">
<head>
<meta charset="utf-8">
<title>{title}</title>
<style>{RESULT_PAGE_CSS}</style>
</head>
<body>
<div class="card">
  {RESULT_PAGE_MARK}
  <h1 style="color:{tone}">{title}</h1>
  <div class="body">{body}</div>
  {RESULT_PAGE_BACK}
</div>
{RESULT_PAGE_THEME_JS}
</body>
</html>""")


@router.get("/oauth-callback", response_class=HTMLResponse,
            summary="OAuth callback for Google Gmail authorization")
def gmail_oauth_callback(code: str | None = None, error: str | None = None):
    def page(title: str, body: str, ok: bool = False) -> HTMLResponse:
        return result_page(title, body, ok)

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
