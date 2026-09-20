"""SDOC Shipping Document Verification — Backend API.

FastAPI entry point. Run locally:

    cd backend
    uvicorn app.main:app --reload --port 8000

Interactive docs: http://localhost:8000/docs
"""

from __future__ import annotations

import json
import logging
import asyncio
from datetime import datetime
from pathlib import Path

from fastapi import Depends, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from sqlalchemy.orm import Session

from app.config import get_settings
from app.database import SessionLocal, get_db, init_db
from app.models import EmailRecord
from app.routers import (
    ai_assist,
    emails,
    frontend_compat,
    gateway,
    gmail,
    ingest,
    integration,
    reports,
    reviews,
    shipments,
)


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

settings = get_settings()
gmail_poll_task = None

app = FastAPI(
    title=settings.app_name,
    version=settings.app_version,
    description=(
        "Email inbox → classification → SI/BL attachment extraction → "
        "deterministic 7-field comparison → discrepancy report with "
        "human-in-the-loop review."
    ),
)


app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


app.include_router(emails.router)
app.include_router(reports.router)
app.include_router(reviews.router)
app.include_router(shipments.router)
app.include_router(ai_assist.router)
app.include_router(ingest.router)
app.include_router(integration.router)
app.include_router(gmail.router)
app.include_router(frontend_compat.router)
app.include_router(gateway.router)



# Frontends served by this single backend (no extra Render service):
#   /ui/       -> ShipSync Dashboard / Operations Console (existing web app)
#   /shipmail/ -> ShipMail inbox (simulated mail client) + ShipSync side panel
WEB_DIR = Path(__file__).resolve().parent.parent / "web"
SHIPMAIL_DIR = Path(__file__).resolve().parent.parent / "shipmail" / "frontend"
SHIPMAIL_DATA_DIR = Path(__file__).resolve().parent.parent / "shipmail" / "data"

from fastapi.staticfiles import StaticFiles  # noqa: E402


class SafeStaticFiles(StaticFiles):
    """Static file serving that answers 404 instead of crashing.

    On Windows, ``os.stat()`` on a path containing ``*`` or ``?`` raises
    ``OSError(WinError 123)`` rather than ``FileNotFoundError``. Starlette only
    expects the latter, so a request such as ``/shipmail/**`` escaped as an
    unhandled exception and the user saw a bare "Internal Server Error" over an
    otherwise healthy app. Treating any odd path as "not found" keeps the UI
    reachable no matter what URL a browser, scanner or typo throws at it.
    """

    def lookup_path(self, path: str):
        try:
            return super().lookup_path(path)
        except OSError:
            return "", None

    async def get_response(self, path: str, scope):
        """Fall back to the app shell for navigation-style URLs.

        A URL such as ``/shipmail/**`` (a stray ``*``, a bad copy/paste, a
        scanner probe) is clearly meant to be a page, not a file, so serving
        ``index.html`` is far more useful than a JSON 404. Requests for real
        files that are genuinely missing still 404 normally.
        """
        try:
            return await super().get_response(path, scope)
        except StarletteHTTPException as exc:
            last = path.rsplit("/", 1)[-1]
            if exc.status_code == 404 and self.html and "." not in last:
                return await super().get_response("", scope)
            raise


if WEB_DIR.is_dir():
    app.mount(
        "/ui",
        SafeStaticFiles(directory=str(WEB_DIR), html=True),
        name="ui",
    )

if SHIPMAIL_DATA_DIR.is_dir():
    # Static demo inbox (emails.json / shipments.json / attachments). Served as
    # plain files: opening the inbox costs no database or pipeline work.
    # Mounted BEFORE /shipmail so the more specific prefix wins.
    app.mount(
        "/shipmail/data",
        SafeStaticFiles(directory=str(SHIPMAIL_DATA_DIR)),
        name="shipmail-data",
    )

if SHIPMAIL_DIR.is_dir():
    app.mount(
        "/shipmail",
        SafeStaticFiles(directory=str(SHIPMAIL_DIR), html=True),
        name="shipmail",
    )


@app.exception_handler(StarletteHTTPException)
async def _http_error(request: Request, exc: StarletteHTTPException):
    """Show humans a readable 404 instead of ``{"detail": "Not Found"}``."""
    wants_html = "text/html" in (request.headers.get("accept") or "")
    if exc.status_code != 404 or not wants_html:
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})
    return HTMLResponse(
        status_code=404,
        content=(
            "<!doctype html><meta charset='utf-8'><title>Not found</title>"
            "<body style=\"font:15px/1.6 system-ui;background:#0b1020;color:#e5e7eb;"
            "display:flex;align-items:center;justify-content:center;height:100vh;margin:0\">"
            "<div style=\"max-width:560px;background:#131a2e;border:1px solid #2a3555;"
            "border-radius:14px;padding:28px 32px\">"
            "<h1 style='margin:0 0 10px;font-size:19px;color:#fbbf24'>Page not found</h1>"
            f"<div style='color:#c7cfe3'><code>{request.url.path}</code> does not exist.</div>"
            "<p style='margin:16px 0 0'><a href='/shipmail/' style='color:#7dd3fc'>"
            "Go to ShipMail</a> &middot; "
            "<a href='/ui/' style='color:#7dd3fc'>Dashboard</a></p></div></body>"
        ),
    )


@app.exception_handler(Exception)
async def _unhandled_error(request: Request, exc: Exception):
    """Last-resort handler: never show a bare "Internal Server Error".

    Anything that still escapes is logged in full and rendered as a readable
    page carrying the failure reason, so a demo never dead-ends on an
    unreadable white screen.
    """
    import traceback

    tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    logger = logging.getLogger("sdoc.errors")
    logger.error("Unhandled error on %s %s\n%s", request.method, request.url.path, tb)
    tail = "".join(tb.strip().splitlines(keepends=True)[-6:])
    return HTMLResponse(
        status_code=500,
        content=(
            "<!doctype html><meta charset='utf-8'><title>ShipSync error</title>"
            "<body style=\"font:15px/1.6 system-ui;background:#0b1020;color:#e5e7eb;"
            "display:flex;align-items:center;justify-content:center;height:100vh;margin:0\">"
            "<div style=\"max-width:680px;background:#131a2e;border:1px solid #2a3555;"
            "border-radius:14px;padding:28px 32px\">"
            "<h1 style='margin:0 0 10px;font-size:19px;color:#f87171'>ShipSync hit an error</h1>"
            f"<div style='color:#c7cfe3'>Path: <code>{request.url.path}</code></div>"
            f"<pre style='white-space:pre-wrap;background:#0b1020;border:1px solid #2a3555;"
            f"border-radius:8px;padding:12px;color:#fca5a5;font-size:12px;overflow:auto'>"
            f"{tail}</pre>"
            "<p style='margin:14px 0 0'>The full traceback is in the server log.</p>"
            "<p style='margin:8px 0 0'><a href='/shipmail/' style='color:#7dd3fc'>"
            "Back to ShipMail</a></p></div></body>"
        ),
    )


def _parse_received_at(value):
    """Convert common JSON datetime formats into a Python datetime."""
    if not value:
        return None

    if isinstance(value, datetime):
        return value

    if not isinstance(value, str):
        return None

    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def seed_email_records() -> None:
    """
    Import the static inbox JSON files into EmailRecord.

    This intentionally does NOT process attachments, run OCR, classify emails,
    or generate reports. It only makes the inbox available to the Review Desk.

    This is lightweight enough to run during application startup on Render's
    Free instance.
    """
    inbox_dir = Path(settings.data_source) / "inbox"

    if not inbox_dir.is_dir():
        logging.warning(
            "Inbox directory not found: %s. "
            "Skipping email database seeding.",
            inbox_dir,
        )
        return

    email_files = sorted(inbox_dir.glob("email_*.json"))

    if not email_files:
        logging.warning(
            "No email JSON files found in %s.",
            inbox_dir,
        )
        return

    db = SessionLocal()

    try:
        existing_ids = {
            row.email_id
            for row in db.query(EmailRecord.email_id).all()
        }

        added = 0

        for path in email_files:
            try:
                with path.open("r", encoding="utf-8") as f:
                    data = json.load(f)
            except Exception as exc:
                logging.warning(
                    "Could not read inbox file %s: %s",
                    path,
                    exc,
                )
                continue

            email_id = (
                data.get("email_id")
                or data.get("id")
                or data.get("message_id")
                or path.stem
            )

            if not email_id:
                logging.warning(
                    "Skipping %s because no email ID was found.",
                    path,
                )
                continue

            email_id = str(email_id)

            if email_id in existing_ids:
                continue

            sender = (
                data.get("sender")
                or data.get("from")
                or data.get("email")
                or ""
            )

            subject = data.get("subject") or ""

            body = (
                data.get("body")
                or data.get("text")
                or data.get("content")
                or ""
            )

            attachments = (
                data.get("attachments")
                or data.get("files")
                or []
            )

            if not isinstance(attachments, list):
                attachments = [str(attachments)]

            received_at = _parse_received_at(
                data.get("received_at")
                or data.get("timestamp")
                or data.get("date")
            )

            record = EmailRecord(
                email_id=email_id,
                sender=str(sender),
                subject=str(subject),
                body=str(body),
                attachments=attachments,
                received_at=received_at,
            )

            db.add(record)
            existing_ids.add(email_id)
            added += 1

        if added:
            db.commit()

        logging.info(
            "Inbox database seeding complete: %s new emails imported, "
            "%s JSON files found.",
            added,
            len(email_files),
        )

    except Exception:
        db.rollback()
        raise

    finally:
        db.close()


@app.on_event("startup")
def on_startup() -> None:
    """
    Initialize the database and import lightweight inbox metadata.

    Heavy document processing is intentionally skipped during startup.
    This prevents the Render Free instance from exceeding its 512 MB
    memory limit.
    """
    init_db()

    try:
        seed_email_records()
    except Exception as exc:
        logging.warning(
            "Inbox database seeding failed: %s",
            exc,
        )

    logging.info(
        "Database initialized. "
        "Heavy inbox processing skipped during startup."
    )

    global gmail_poll_task
    if settings.gmail_polling_enabled:
        gmail_poll_task = asyncio.create_task(_gmail_poll_loop())


@app.on_event("shutdown")
async def on_shutdown() -> None:
    global gmail_poll_task
    if gmail_poll_task:
        gmail_poll_task.cancel()
        try:
            await gmail_poll_task
        except asyncio.CancelledError:
            pass


async def _gmail_poll_loop() -> None:
    """Optional background poller for the dedicated Gmail demo account."""
    from app.integrations.gmail import sync as gmail_sync

    while True:
        db = SessionLocal()
        try:
            gmail_sync.poll_and_process(db, limit=settings.gmail_max_results)
        except Exception as exc:  # noqa: BLE001
            logging.warning("Gmail polling failed: %s", exc)
        finally:
            db.close()
        await asyncio.sleep(max(settings.gmail_poll_interval_seconds, 10))


@app.get("/", tags=["meta"])
def root():
    from fastapi.responses import RedirectResponse

    return RedirectResponse(url="/shipmail/")


@app.get("/meta", tags=["meta"])
def meta():
    return {
        "service": settings.app_name,
        "version": settings.app_version,
        "docs": "/docs",
        "endpoints": [
            "GET  /emails",
            "GET  /emails/{email_id}",
            "POST /emails/{email_id}/process",
            "POST /emails/process-all",
            "GET  /reports",
            "GET  /reports/{report_id_or_email_id}",
            "GET  /reports/summary/stats",
            "GET  /reports/submission/json",
            "POST /reviews/{email_id}",
            "GET  /shipments",
            "GET  /shipments/{shipment_id}",
            "GET  /shipments/{shipment_id}/versions",
            "GET  /shipments/{shipment_id}/version-diff",
            "GET  /shipments/{shipment_id}/issues",
            "POST /issues/{issue_id}/approve",
            "POST /issues/{issue_id}/reject",
            "POST /shipments/{shipment_id}/generate-corrected-draft",
            "GET  /api/health",
            "POST /api/process",
            "GET  /api/results           (stored results — never reprocesses)",
            "GET  /api/results/{key}     (email_id | message_id | cache_key)",
            "POST /api/mock-customer-db/request-document",
            "POST /api/v1/analyze",
            "POST /api/v1/ingest",
            "GET  /api/gmail/status",
            "GET  /api/gmail/connect",
            "POST /api/gmail/poll",
            "GET  /api/gmail/messages",
            "GET  /api/gmail/attachment/{email_id}/{filename}",
            "GET  /ai/issues/{issue_id}/explain",
            "GET  /ai/issues/{issue_id}/suggest",
            "GET  /ai/shipments/{shipment_id}/correction-email",
            "POST /ai/ambiguous-interpretation",
            "GET  /health",
            "GET  /ui/  (P1 frontend: Review Desk)",
            "GET  /shipmail/  (ShipMail inbox + ShipSync side panel)",
            "GET  /api/summary, /api/emails, /api/emails/{id}, "
            "/api/emails/{id}/review, /api/review-queue, "
            "/api/attachments/{path}",
        ],
    }


@app.get("/health", tags=["meta"])
def health(db: Session = Depends(get_db)):
    from app.models import EmailRecord, ReportRecord

    return {
        "status": "ok",
        "app_name": settings.app_name,
        "version": settings.app_version,
        "environment": settings.environment,
        "database": settings.database_url.split("://")[0],
        "data_source": settings.data_source,
        "ai_provider": settings.ai_provider,
        "emails_cached": db.query(EmailRecord).count(),
        "reports_stored": db.query(ReportRecord).count(),
    }
