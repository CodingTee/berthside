"""SDOC Shipping Document Verification — Backend API.

FastAPI entry point. Run locally:

    cd backend
    uvicorn app.main:app --reload --port 8000

Interactive docs: http://localhost:8000/docs
"""
from __future__ import annotations

import logging
from pathlib import Path

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import Session

from app.config import get_settings
from app.database import SessionLocal, get_db, init_db
from app.routers import emails, frontend_compat, reports, reviews

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")

settings = get_settings()

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
app.include_router(frontend_compat.router)  # P1 frontend contract (/api/*)

# Frontend (P1) UI — the friend's Review Desk, served from /ui/
WEB_DIR = Path(__file__).resolve().parent.parent / "web"
if WEB_DIR.is_dir():
    from fastapi.staticfiles import StaticFiles
    app.mount("/ui", StaticFiles(directory=str(WEB_DIR), html=True), name="ui")


@app.on_event("startup")
def on_startup() -> None:
    init_db()
    # Pre-process the whole inbox once so the Review Desk UI is populated on
    # first launch (idempotent — later starts skip this when reports exist).
    try:
        db = SessionLocal()
        try:
            if db.query(__import__("app.models", fromlist=["ReportRecord"])
                       .ReportRecord).count() == 0:
                from app.services import workflow
                stats = workflow.process_all(db, limit=settings.process_max_emails)
                logging.info("startup pre-processed inbox: %s", stats)
        finally:
            db.close()
    except Exception as exc:  # noqa: BLE001 — never let startup crash the API
        logging.warning("startup pre-processing skipped: %s", exc)


@app.get("/", tags=["meta"])
def root():
    from fastapi.responses import RedirectResponse
    return RedirectResponse(url="/ui/")


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
            "GET  /health",
            "GET  /ui/  (P1 frontend: Review Desk)",
            "GET  /api/summary, /api/emails, /api/emails/{id}, "
            "/api/emails/{id}/review, /api/review-queue, /api/attachments/{path}",
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
