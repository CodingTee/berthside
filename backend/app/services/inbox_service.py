"""Inbox access — wraps the official loader.py so the rest of the app never
touches the data source directly (local folder or HTTP server, same API).
"""
from __future__ import annotations

from functools import lru_cache
from typing import Iterator, Optional

from app.services import loader


@lru_cache(maxsize=1)
def _inbox():
    source = _settings().data_source
    return loader.Inbox(source)


def _settings():
    from app.config import get_settings
    return get_settings()


def all_emails() -> list[dict]:
    return _inbox().emails()


def get_email(email_id: str) -> Optional[dict]:
    try:
        return _inbox().get(email_id)
    except FileNotFoundError:
        return None
    except Exception:  # HTTP 404 from the dataset server
        return None


def read_attachment(att_path: str) -> bytes:
    return _inbox().read_bytes(att_path)


def read_attachment_text(att_path: str) -> str:
    return _inbox().read_text(att_path)


def sample_submission() -> dict:
    return _inbox().sample_submission()


def refresh() -> None:
    """Drop the cached inbox (e.g. after switching DATA_SOURCE in tests)."""
    _inbox.cache_clear()
