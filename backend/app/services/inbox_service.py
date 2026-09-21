"""Inbox access — wraps the official loader.py so the rest of the app never
touches the data source directly (local folder or HTTP server, same API).
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, Iterator, Optional

from app.services import loader

if TYPE_CHECKING:
    from sqlalchemy.orm import Session


@lru_cache(maxsize=1)
def _inbox():
    source = _settings().data_source
    return loader.Inbox(source)


def _settings():
    from app.config import get_settings
    return get_settings()


def all_emails(db: Optional[Session] = None,
               include_ingested: bool = True) -> list[dict]:
    """The inbox, optionally merged with emails ingested through the API.

    Merging is what makes an ingested email appear in the Review Desk, so it
    stays on by default. `include_ingested=False` returns exactly what the data
    bundle holds, which is what an evaluation of the corpus wants: otherwise
    whatever happens to be sitting in the local dev database silently joins the
    run and the measured corpus is no longer the official one.

    `db` lets a caller supply its own session instead of reaching for the
    application-wide one, so a caller that already built an isolated engine
    stays isolated.
    """
    inbox_list = list(_inbox().emails())
    if not include_ingested:
        return inbox_list

    owns_session = db is None
    try:
        from app.models import EmailRecord

        if owns_session:
            from app.database import SessionLocal
            db = SessionLocal()

        known_ids = {e["email_id"] for e in inbox_list}
        for r in db.query(EmailRecord).all():
            if r.email_id in known_ids:
                continue
            inbox_list.append({
                "email_id": r.email_id,
                "from": r.sender or "",
                "subject": r.subject or "",
                "body": r.body or "",
                "attachments": r.attachments or [],
            })
            known_ids.add(r.email_id)
    except Exception:
        pass
    finally:
        if owns_session and db is not None:
            db.close()
    return inbox_list


def get_email(email_id: str) -> Optional[dict]:
    try:
        res = _inbox().get(email_id)
        if res:
            return res
    except (FileNotFoundError, Exception):
        pass

    # Fallback to database for ingested emails
    try:
        from app.database import SessionLocal
        from app.models import EmailRecord
        with SessionLocal() as db:
            row = db.query(EmailRecord).filter_by(email_id=email_id).first()
            if row:
                # received_at travels with the email because it is part of the
                # source information a shipment carries ("Received: 20 Sep"),
                # so it is handed on rather than dropped here.
                return {
                    "email_id": row.email_id,
                    "from": row.sender or "",
                    "subject": row.subject or "",
                    "body": row.body or "",
                    "attachments": row.attachments or [],
                    "received_at": row.received_at,
                }
    except Exception:
        pass
    return None


def read_attachment(att_path: str) -> bytes:
    p = Path(att_path)
    if p.is_file():
        return p.read_bytes()
    backend_root = Path(__file__).resolve().parent.parent.parent
    candidate = backend_root / att_path
    if candidate.is_file():
        return candidate.read_bytes()
    from app.config import get_settings
    settings = get_settings()
    ingest_cand = Path(settings.ingest_dir) / att_path
    if ingest_cand.is_file():
        return ingest_cand.read_bytes()
    return _inbox().read_bytes(att_path)


def read_attachment_text(att_path: str) -> str:
    p = Path(att_path)
    if p.is_file():
        return p.read_text(encoding="utf-8", errors="replace")
    backend_root = Path(__file__).resolve().parent.parent.parent
    candidate = backend_root / att_path
    if candidate.is_file():
        return candidate.read_text(encoding="utf-8", errors="replace")
    from app.config import get_settings
    settings = get_settings()
    ingest_cand = Path(settings.ingest_dir) / att_path
    if ingest_cand.is_file():
        return ingest_cand.read_text(encoding="utf-8", errors="replace")
    return _inbox().read_text(att_path)



def sample_submission() -> dict:
    return _inbox().sample_submission()


def refresh() -> None:
    """Drop the cached inbox (e.g. after switching DATA_SOURCE in tests)."""
    _inbox.cache_clear()
