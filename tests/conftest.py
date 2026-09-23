"""Test harness: keep every test off the real database and the real disk.

Without this, any test that exercises the stateful endpoints writes to the
developer's `backend/sdoc_enterprise.db` and `backend/data/ingested/`. That already
happened: a stray `EXT-TEST-0099` ended up in the dev database, and because
`inbox_service.all_emails()` merges ingested rows into the inbox, a local
evaluation started reporting 521 emails instead of the official 520.

The same reasoning applies to the mail channel. A developer's `.env` points at a
live mailbox, so a test that returns a document to its sender opened a real SMTP
session and mailed whichever address the fixture had seeded, and an IMAP poll
would have marked real unread mail as seen. Both hosts are blanked below, so a
test can only ever simulate; a test that wants the live branch sets the
attributes on `get_settings()` itself.

Isolation is applied at runtime rather than by rewriting DATABASE_URL, for two
reasons:

* `app/database.py` builds its engine at import time, and by the time a fixture
  runs, pytest has normally imported the app already. A later env change would
  not be picked up.
* `app.dependency_overrides` works regardless of import order, and patching
  `SessionLocal` covers the code paths that reach for the module global (for
  example `inbox_service.get_email`).

`get_settings()` is cached, so every env change here is followed by
`cache_clear()`; that also means the cache is rebuilt per test and the direct
attribute edits in the mail tests cannot leak into the next one.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

BACKEND_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND_ROOT))

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import models  # noqa: F401  registers the tables on Base
from app.database import Base, get_db


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    """Give each test its own database and its own ingest directory.

    StaticPool matters: a plain in-memory SQLite engine hands a separate
    database to each thread, and the test client runs the app in a worker
    thread, so the request would see an empty schema.
    """
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    testing_session = sessionmaker(bind=engine, autocommit=False, autoflush=False)

    import app.database as database
    monkeypatch.setattr(database, "SessionLocal", testing_session)

    from app.config import get_settings
    monkeypatch.setenv("INGEST_DIR", str(tmp_path / "ingested"))
    for var in (
        "SMTP_HOST", "SMTP_USER", "SMTP_PASSWORD",
        "IMAP_HOST", "IMAP_USER", "IMAP_PASSWORD",
    ):
        monkeypatch.setenv(var, "")
    get_settings.cache_clear()

    from app.main import app as fastapi_app

    def _test_session():
        db = testing_session()
        try:
            yield db
        finally:
            db.close()

    fastapi_app.dependency_overrides[get_db] = _test_session
    try:
        yield
    finally:
        fastapi_app.dependency_overrides.pop(get_db, None)
        get_settings.cache_clear()
        engine.dispose()


@pytest.fixture()
def db_session():
    """Direct DB session for tests that exercise service-layer code."""
    import app.database as database

    db = database.SessionLocal()
    try:
        yield db
    finally:
        db.close()
