"""Real Gmail integration endpoints."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, Response
from pathlib import Path
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.config import get_settings
from app.database import get_oauth_db
from app.integrations.gmail import auth, sync
from app.models import (
    DispatchRecord,
    EmailRecord,
    GmailMessageRecord,
    ReportRecord,
    ShipmailAssignmentRecord,
)
from app.routers import integration
from app.schemas import AttachmentPayload, EmailAnalyzeRequest, EmailProcessResponse
from app.services import inbox_service

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

# On a successful connect the callback writes a marker into localStorage. The
# ShipMail page (a different tab/window of the same origin) hears it via the
# `storage` event and runs the single initial sync — so Connect Gmail performs
# the sync automatically, with no manual "Sync Mail" click. window.close() is
# allowed here because the tab was opened by window.open().
RESULT_PAGE_CONNECT_JS = (
    "<script>try{localStorage.setItem(\"sdoc-gmail-connected\", "
    "String(Date.now()));}catch(_){}"
    "try{setTimeout(function(){window.close();},1200);}catch(_){}</script>"
)


def result_page(title: str, body: str, ok: bool = False,
                extra_js: str = "") -> HTMLResponse:
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
{extra_js}
{RESULT_PAGE_THEME_JS}
</body>
</html>""")


@router.get("/oauth-callback", response_class=HTMLResponse,
            summary="OAuth callback for Google Gmail authorization")
def gmail_oauth_callback(code: str | None = None, error: str | None = None):
    def page(title: str, body: str, ok: bool = False,
             extra_js: str = "") -> HTMLResponse:
        return result_page(title, body, ok, extra_js)

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
                "Gmail is connected — your inbox is syncing now. You can close "
                "this tab.", True, RESULT_PAGE_CONNECT_JS)


@router.post("/poll", summary="Poll Gmail inbox and process unseen messages")
def poll_gmail(
    limit: int = Query(10, ge=1, le=50),
    force: bool = Query(False, description="Re-process already synced messages too"),
    db: Session = Depends(get_oauth_db),
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
    db: Session = Depends(get_oauth_db),
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
        att_paths = []
        if email and email.attachments:
            for p in email.attachments:
                att_paths.append(p or "")
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
            # Parallel to `attachments`: the stored path, which is what the
            # workspace needs to open or download the file.
            "attachment_paths": att_paths,
        })
    return {"total": len(items), "items": items}


@router.get("/attachment/{email_id}/{filename:path}",
            summary="Read a synced Gmail attachment by filename")
def gmail_attachment(email_id: str, filename: str, db: Session = Depends(get_oauth_db)):
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


# ------------------------------------------- OAuth-scoped ShipMail compat
# ShipMail drives the same pipeline as the Review Console, but every row it
# reads or writes has to land in the OAuth database: synced operator mail is
# the ShipMail workspace's own data, and the hub's 520-email corpus stays
# untouched by it. The handlers below are the console's own, called with an
# OAuth session instead of the default hub one.

@router.post("/process", response_model=EmailProcessResponse,
             summary="Process one email inside the ShipMail workspace")
def oauth_process_email(
    payload: EmailAnalyzeRequest,
    db: Session = Depends(get_oauth_db),
) -> EmailProcessResponse:
    """Same pipeline and dedup contract as POST /api/process, stored in the
    OAuth database so a ShipMail run never writes a hub shipment."""
    return integration.process_email(payload, db=db)


@router.get("/results/{key}", response_model=EmailProcessResponse,
            summary="Read one stored result from the ShipMail workspace")
def oauth_get_result(
    key: str,
    db: Session = Depends(get_oauth_db),
) -> EmailProcessResponse:
    return integration.get_result(key, db=db)


@router.get("/emails", summary="List stored emails in the ShipMail workspace")
def oauth_list_emails(
    category: str | None = None,
    status: str | None = None,
    q: str | None = None,
    limit: int = Query(500, ge=1, le=500),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_oauth_db),
):
    """Same wire shape as GET /api/emails, limited to rows synced through
    Gmail. The console's corpus is not part of this list."""
    rep_map = {r.email_id: r for r in db.query(ReportRecord).all()}

    items = []
    for row in db.query(EmailRecord).order_by(EmailRecord.id.desc()).all():
        r = rep_map.get(row.email_id)
        if category and (not r or r.category != category):
            continue
        if status and (not r or r.status != status):
            continue
        if q:
            hay = f"{row.email_id} {row.sender or ''} {row.subject or ''}".lower()
            if q.lower() not in hay:
                continue
        items.append({
            "email_id": row.email_id,
            "from": row.sender or "",
            "subject": row.subject or "",
            "n_attachments": len(row.attachments or []),
            "category": r.category if r else None,
            "status": r.status if r else None,
            "has_defect": bool(r.has_defect) if r else False,
            "defect_fields": (r.defect_fields or []) if r else [],
            "review_reason": r.review_reason if r else None,
            "decided_by": "human" if (r and r.reviewed) else "rule",
        })

    total = len(items)
    return {"total": total, "items": items[offset:offset + limit]}


@router.get("/attachments/{rel_path:path}",
            summary="Read an attachment of a synced Gmail message")
def oauth_attachment(
    rel_path: str,
    db: Session = Depends(get_oauth_db),
):
    """Serve one attachment file, but only if a synced message references it.

    The ownership check is what keeps this endpoint inside the workspace: a
    path that belongs to the hub corpus is not in any OAuth EmailRecord, so it
    404s here instead of leaking through.
    """
    owned = (
        db.query(EmailRecord)
        .filter(EmailRecord.attachments.isnot(None))
        .all()
    )
    if not any(rel_path in (row.attachments or []) for row in owned):
        raise HTTPException(404, "attachment not found in the ShipMail workspace")

    text_suffixes = {".txt", ".csv", ".md", ".json", ".xml", ".log"}
    try:
        if Path(rel_path).suffix.lower() in text_suffixes:
            return Response(inbox_service.read_attachment_text(rel_path),
                            media_type="text/plain")
        return Response(inbox_service.read_attachment(rel_path),
                        media_type="application/octet-stream")
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(404, "attachment file missing") from exc


# ------------------------------------------- OAuth-scoped review console
# The hub distributes mail per source mailbox. This workspace has a single
# source, the linked Gmail account, so the distribution decision is per email
# instead: AUTO clears a message, HOLD parks it for a human verdict, IGNORE
# takes it out of the actionable queue. Every read and write below runs on the
# OAuth session, so an action taken in this console can never reach the hub
# corpus, and the hub console can never distribute operator mail.

ASSIGNMENTS = ("AUTO", "HOLD", "IGNORE")
DEFAULT_ASSIGNMENT = "HOLD"


class AssignIn(BaseModel):
    email_ids: list[str]
    assignment: str
    note: str | None = None


class BulkRunIn(BaseModel):
    email_ids: list[str] = []
    include_ignored: bool = False


class OauthReturnIn(BaseModel):
    subject: str | None = None
    body: str | None = None
    dry_run: bool = False


def _workspace_email_ids(db: Session) -> set[str]:
    """Identity guard every console mutation runs through."""
    return {row[0] for row in db.query(EmailRecord.email_id).all()}


def _assignment_map(db: Session) -> dict[str, ShipmailAssignmentRecord]:
    return {r.email_id: r for r in db.query(ShipmailAssignmentRecord).all()}


def _missing_documents(report: ReportRecord | None) -> list[str]:
    if not report:
        return []
    return list((report.extracted or {}).get("missing_documents") or [])


def _attachment_names(paths) -> list[str]:
    return [(p or "").rsplit("/", 1)[-1].rsplit("\\", 1)[-1] for p in (paths or [])]


def _tally(rows: list[dict], key: str) -> dict[str, int]:
    out: dict[str, int] = {}
    for row in rows:
        value = row.get(key)
        if value:
            out[value] = out.get(value, 0) + 1
    return out


def _review_rows(db: Session) -> list[dict]:
    """Every synced message with its verdict, assignment and dispatch state."""
    reports = {r.email_id: r for r in db.query(ReportRecord).all()}
    gmail = {g.email_id: g for g in db.query(GmailMessageRecord).all()}
    assignments = _assignment_map(db)
    dispatched = {d.stage_id for d in db.query(DispatchRecord).all()}

    rows = []
    for row in db.query(EmailRecord).order_by(EmailRecord.id.desc()).all():
        report = reports.get(row.email_id)
        msg = gmail.get(row.email_id)
        asg = assignments.get(row.email_id)
        ex = (report.extracted or {}) if report else {}
        ship_key = str(ex.get("shipment_key") or "")
        rows.append({
            "email_id": row.email_id,
            "sender": row.sender or (msg.sender if msg else "") or "",
            "subject": row.subject or (msg.subject if msg else "") or "",
            "snippet": (row.body or "")[:140],
            "received_at": (row.received_at or (msg.created_at if msg else None)),
            "category": report.category if report else None,
            "status": report.status if report else None,
            "has_defect": bool(report.has_defect) if report else False,
            "defect_fields": list(report.defect_fields or []) if report else [],
            "review_reason": report.review_reason if report else None,
            "decided_by": ("human" if report.reviewed else "rule") if report else None,
            "n_attachments": len(row.attachments or []),
            "attachments": _attachment_names(row.attachments),
            "attachment_paths": list(row.attachments or []),
            "missing_documents": _missing_documents(report),
            "shipment": (ship_key.split(":")[-1] if ship_key else ex.get("shipment_id")),
            "assignment": asg.assignment if asg else DEFAULT_ASSIGNMENT,
            "note": asg.note if asg else None,
            "assigned_at": asg.updated_at if asg else None,
            "dispatched": row.email_id in dispatched,
            "processing_status": msg.processing_status if msg else None,
        })
    return rows


def _review_kpis(rows: list[dict]) -> dict[str, int]:
    return {
        "total": len(rows),
        "processed": sum(1 for r in rows if r["status"]),
        "awaiting": sum(1 for r in rows if r["assignment"] == "HOLD"),
        "mismatch": sum(1 for r in rows if r["status"] == "MISMATCH"),
        "missing_docs": sum(1 for r in rows
                            if r["missing_documents"] or r["category"] == "SI_REQUEST"),
        "auto": sum(1 for r in rows if r["assignment"] == "AUTO"),
        "ignored": sum(1 for r in rows if r["assignment"] == "IGNORE"),
    }


@router.get("/review", summary="Review console payload for the ShipMail workspace")
def oauth_review(
    category: str | None = None,
    status: str | None = None,
    assignment: str | None = None,
    q: str | None = None,
    limit: int = Query(500, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_oauth_db),
):
    """The whole review console in one call: KPI counters over every synced
    message, plus the filtered rows the table renders. Filtering happens here
    so the counters stay stable while the operator narrows the list."""
    rows = _review_rows(db)

    items = rows
    if category:
        items = [r for r in items if r["category"] == category]
    if status:
        items = [r for r in items if r["status"] == status]
    if assignment:
        want = assignment.strip().upper()
        items = [r for r in items if r["assignment"] == want]
    if q:
        needle = q.strip().lower()
        items = [
            r for r in items
            if needle in f'{r["email_id"]} {r["sender"]} {r["subject"]}'.lower()
        ]

    return {
        "kpis": _review_kpis(rows),
        "counts": {
            "assignment": {k: sum(1 for r in rows if r["assignment"] == k) for k in ASSIGNMENTS},
            "category": _tally(rows, "category"),
            "status": _tally(rows, "status"),
        },
        "total": len(items),
        "items": items[offset:offset + limit],
    }


@router.post("/assign", summary="Distribute ShipMail emails (auto / hold / ignore)")
def oauth_assign(payload: AssignIn, db: Session = Depends(get_oauth_db)):
    """Set the distribution decision for one or many messages.

    An email_id that is not stored in this workspace is refused rather than
    quietly written, so a malformed request cannot create an assignment for a
    message the OAuth database has never seen.
    """
    want = (payload.assignment or "").strip().upper()
    if want not in ASSIGNMENTS:
        raise HTTPException(400, f"assignment must be one of: {', '.join(ASSIGNMENTS)}")

    owned = _workspace_email_ids(db)
    existing = _assignment_map(db)
    rejected: list[str] = []
    updated = 0

    for email_id in dict.fromkeys(payload.email_ids or []):
        if email_id not in owned:
            rejected.append(email_id)
            continue
        rec = existing.get(email_id)
        if rec is None:
            rec = ShipmailAssignmentRecord(email_id=email_id, assignment=want,
                                           note=payload.note)
            db.add(rec)
            existing[email_id] = rec
        else:
            rec.assignment = want
            if payload.note is not None:
                rec.note = payload.note
        updated += 1

    db.commit()
    return {"assignment": want, "updated": updated, "rejected": rejected}


def _analyze_request(email: EmailRecord) -> EmailAnalyzeRequest:
    """Rebuild the process payload of a stored message, attachments included."""
    import base64

    text_suffixes = {".txt", ".csv", ".md", ".json", ".xml", ".log"}
    payload_atts: list[AttachmentPayload] = []
    for path in (email.attachments or []):
        name = (path or "").rsplit("/", 1)[-1].rsplit("\\", 1)[-1] or "attachment"
        try:
            if Path(path).suffix.lower() in text_suffixes:
                payload_atts.append(AttachmentPayload(
                    filename=name, content_text=inbox_service.read_attachment_text(path)))
            else:
                raw = inbox_service.read_attachment(path)
                payload_atts.append(AttachmentPayload(
                    filename=name, content_base64=base64.b64encode(raw).decode("ascii")))
        except Exception:  # noqa: BLE001 - a missing file must not kill the batch
            continue

    return EmailAnalyzeRequest(
        email_id=email.email_id,
        sender=email.sender or "unknown@sender",
        subject=email.subject or "",
        body=email.body or "",
        attachments=payload_atts,
        source="shipmail",
    )


@router.post("/bulk-run", summary="Run the pipeline for the selected ShipMail emails")
def oauth_bulk_run(payload: BulkRunIn, db: Session = Depends(get_oauth_db)):
    """Explicit bulk processing, never automatic.

    Messages distributed as IGNORE are skipped: the operator already decided
    they are not actionable, so a bulk run must not quietly spend pipeline time
    on them unless the caller asks for it.
    """
    owned = _workspace_email_ids(db)
    assignments = _assignment_map(db)
    results: list[dict] = []
    skipped: list[dict] = []

    for email_id in dict.fromkeys(payload.email_ids or []):
        if email_id not in owned:
            skipped.append({"email_id": email_id, "reason": "not in this workspace"})
            continue
        asg = assignments.get(email_id)
        if asg and asg.assignment == "IGNORE" and not payload.include_ignored:
            skipped.append({"email_id": email_id, "reason": "assigned IGNORE"})
            continue
        email = db.query(EmailRecord).filter_by(email_id=email_id).first()
        if email is None:
            skipped.append({"email_id": email_id, "reason": "no stored message"})
            continue
        try:
            data = integration.process_email(_analyze_request(email), db=db)
        except HTTPException as exc:
            results.append({"email_id": email_id, "status": None,
                            "error": str(exc.detail)})
            continue
        results.append({"email_id": email_id, "status": data.status,
                        "category": data.category, "cached": data.cached})

    return {"processed": len(results), "skipped": skipped, "results": results}


def _build_shipmail_receipt(email: EmailRecord, report: ReportRecord | None):
    """Compose the reply for one ShipMail message.

    A missing document, a field mismatch or an unverifiable transmission gets a
    clarification request; a clean verification gets the clearance notice. The
    reply is addressed to the original sender and leaves through the linked
    Gmail account, so the round trip stays on the channel it arrived on.
    """
    missing = _missing_documents(report)
    status = (report.status if report else None) or ""
    subject_line = (email.subject or "your transmission").strip()

    if missing:
        docs = " and the ".join(missing)
        subject = f"Re: {subject_line} - {docs} still required"
        body = (
            f"Dear Sender,\n\n"
            f"Thank you for your message. We cannot finish the verification yet because "
            f"the {docs} is not attached.\n\n"
            f"Please reply in this thread with the {docs} attached. We will then compare "
            f"the shipment fields against the documents already on file.\n\n"
            f"Best regards,\nShipSync Operations"
        )
        return "REJECTED", subject, body

    if status == "MISMATCH":
        fields = ", ".join(report.defect_fields or []) or "one or more fields"
        subject = f"Re: {subject_line} - Verification mismatch"
        body = (
            f"Dear Sender,\n\n"
            f"The documents have been checked against each other and these fields do not "
            f"agree: {fields}.\n\n"
            f"Please confirm the correct values so the shipment can be cleared.\n\n"
            f"Best regards,\nShipSync Operations"
        )
        return "REJECTED", subject, body

    if status == "OK":
        subject = f"Re: {subject_line} - Verification complete"
        body = (
            f"Dear Sender,\n\n"
            f"Your documents have been checked against each other and every compared field "
            f"agrees. Nothing further is required from you.\n\n"
            f"Best regards,\nShipSync Operations"
        )
        return "VERIFIED", subject, body

    reason = ((report.review_reason if report else None) or
              "the transmission could not be verified automatically").replace("_", " ")
    subject = f"Re: {subject_line} - More detail required"
    body = (
        f"Dear Sender,\n\n"
        f"More detail is required before this shipment can be verified: {reason}.\n\n"
        f"Please resend with the shipping instruction and the bill of lading attached.\n\n"
        f"Best regards,\nShipSync Operations"
    )
    return "REJECTED", subject, body


@router.post("/return", summary="Return a verification outcome to the sender")
def oauth_return(
    email_id: str,
    payload: OauthReturnIn,
    db: Session = Depends(get_oauth_db),
):
    """Send the outcome of one review back to the original sender.

    ``dry_run`` only renders the draft, which is what the console previews.
    The real call persists a :class:`DispatchRecord` in the OAuth database.
    Operator mail has no live SMTP behind it in this build, so delivery is
    ``SIMULATED``; a Gmail-linked account would send it and report ``SENT``.
    """
    email = db.query(EmailRecord).filter_by(email_id=email_id).first()
    if email is None:
        raise HTTPException(404, "email not found in the ShipMail workspace")

    report = db.query(ReportRecord).filter_by(email_id=email_id).first()
    decision, default_subject, default_body = _build_shipmail_receipt(email, report)
    subject = payload.subject or default_subject
    body = payload.body or default_body

    account = auth.integration_status()["account"]
    missing = _missing_documents(report)

    if payload.dry_run:
        return {
            "email_id": email_id,
            "reply_to": email.sender,
            "reply_via": account,
            "decision": decision,
            "missing_documents": missing,
            "subject": subject,
            "body": body,
            "dry_run": True,
        }

    msg = db.query(GmailMessageRecord).filter_by(email_id=email_id).first()
    rec = DispatchRecord(
        stage_id=email_id,
        source_mailbox=account,
        recipient=email.sender,
        decision=decision,
        subject=subject,
        body=body,
        channel="gmail",
        delivery="SIMULATED",
        gmail_message_id=msg.gmail_message_id if msg else None,
    )
    db.add(rec)
    db.commit()
    db.refresh(rec)

    return {
        "message": "Return dispatched to the sender",
        "email_id": email_id,
        "dispatch_id": rec.id,
        "reply_to": email.sender,
        "reply_via": account,
        "decision": decision,
        "delivery": rec.delivery,
        "subject": subject,
        "body": body,
    }


@router.get("/dispatches", summary="History of returns sent from the ShipMail workspace")
def oauth_dispatches(
    email_id: str | None = None,
    limit: int = Query(50, ge=1, le=200),
    db: Session = Depends(get_oauth_db),
):
    """Audit trail of the returns this workspace sent. Scoped to messages the
    OAuth database owns, so the hub's own dispatches stay out of this list."""
    owned = _workspace_email_ids(db)
    query = db.query(DispatchRecord)
    if email_id:
        query = query.filter(DispatchRecord.stage_id == email_id)
    rows = [r for r in query.order_by(DispatchRecord.id.desc()).all()
            if r.stage_id in owned][:limit]
    return {
        "total": len(rows),
        "items": [{
            "dispatch_id": r.id,
            "email_id": r.stage_id,
            "recipient": r.recipient,
            "reply_via": r.source_mailbox,
            "decision": r.decision,
            "subject": r.subject,
            "delivery": r.delivery,
            "created_at": r.created_at,
        } for r in rows],
    }

