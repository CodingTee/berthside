"""Real Gmail integration endpoints."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from app.database import get_db
from app.integrations.gmail import auth, sync

router = APIRouter(prefix="/api/gmail", tags=["gmail-integration"])


@router.get("/status", summary="Check Gmail integration configuration")
def gmail_status():
    return auth.integration_status()


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
    if error:
        raise HTTPException(400, f"Google OAuth failed: {error}")
    if not code:
        raise HTTPException(400, "missing OAuth code")
    try:
        result = auth.exchange_code_for_token(code)
    except FileNotFoundError as exc:
        raise HTTPException(409, str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(501, str(exc)) from exc
    return HTMLResponse(
        "<h1>ShipSync Gmail connected</h1>"
        f"<p>Account: {result['account']}</p>"
        "<p>You can close this tab and run Gmail polling.</p>"
    )


@router.post("/poll", summary="Poll Gmail inbox and process unseen messages")
def poll_gmail(
    limit: int = Query(10, ge=1, le=50),
    db: Session = Depends(get_db),
):
    try:
        return sync.poll_and_process(db, limit=limit)
    except FileNotFoundError as exc:
        raise HTTPException(409, str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(501, str(exc)) from exc
