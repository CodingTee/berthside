"""Database engine + session factory.

Dual-Track Physical Database Isolation:
1. Enterprise Hub DB (default get_db): stores 520 dataset, carrier EDI, gateway quarantine, audits.
2. OAuth DB (get_oauth_db): stores personal operator Gmail OAuth tokens & synced emails.
"""
from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.orm import declarative_base, sessionmaker

from app.config import get_settings

settings = get_settings()


def _make_engine(url: str):
    connect_args = {}
    if url.startswith("sqlite"):
        # Allow the session to be used across request threads.
        connect_args = {"check_same_thread": False}
    return create_engine(url, connect_args=connect_args, pool_pre_ping=True)


# Enterprise Engine & Session (Default for Central Hub)
enterprise_url = settings.database_url_enterprise or settings.database_url
enterprise_engine = _make_engine(enterprise_url)
EnterpriseSessionLocal = sessionmaker(bind=enterprise_engine, autocommit=False, autoflush=False)

# Backward-compatibility aliases
engine = enterprise_engine
SessionLocal = EnterpriseSessionLocal

# OAuth Engine & Session (Isolated for Operator Mailbox)
oauth_url = settings.database_url_oauth
oauth_engine = _make_engine(oauth_url)
OAuthSessionLocal = sessionmaker(bind=oauth_engine, autocommit=False, autoflush=False)

Base = declarative_base()


def init_db() -> None:
    """Create tables in both databases if they do not exist yet (idempotent)."""
    import app.models  # noqa: F401  (register models on Base)
    Base.metadata.create_all(bind=enterprise_engine)
    Base.metadata.create_all(bind=oauth_engine)
    _migrate_columns(enterprise_engine, EnterpriseSessionLocal)
    _migrate_columns(oauth_engine, OAuthSessionLocal)


def _migrate_columns(eng, session_factory) -> None:
    """Add columns introduced after the first schema release."""
    url_str = str(eng.url)
    if not url_str.startswith("sqlite"):
        return
    from sqlalchemy import text

    with eng.begin() as conn:
        cols = {row[1] for row in conn.execute(text("PRAGMA table_info(emails)")).fetchall()}
        if "source_mailbox" not in cols:
            conn.execute(text("ALTER TABLE emails ADD COLUMN source_mailbox VARCHAR(128)"))
        if "disposition_override" not in cols:
            conn.execute(text("ALTER TABLE emails ADD COLUMN disposition_override VARCHAR(16) DEFAULT 'INHERIT'"))

        gp_cols = {row[1] for row in conn.execute(text("PRAGMA table_info(gateway_policy)")).fetchall()}
        if "source_policies" not in gp_cols:
            conn.execute(text("ALTER TABLE gateway_policy ADD COLUMN source_policies TEXT"))
        if "disposition_mode" not in gp_cols:
            conn.execute(text("ALTER TABLE gateway_policy ADD COLUMN disposition_mode VARCHAR(16) DEFAULT 'manual'"))
        if "disposition_policies" not in gp_cols:
            conn.execute(text("ALTER TABLE gateway_policy ADD COLUMN disposition_policies TEXT"))

        de_cols = {row[1] for row in conn.execute(text("PRAGMA table_info(dispatched_emails)")).fetchall()}
        if "email_id" not in de_cols:
            conn.execute(text("ALTER TABLE dispatched_emails ADD COLUMN email_id VARCHAR(128)"))
        if "gmail_message_id" not in de_cols:
            conn.execute(text("ALTER TABLE dispatched_emails ADD COLUMN gmail_message_id VARCHAR(128)"))
        if "error" not in de_cols:
            conn.execute(text("ALTER TABLE dispatched_emails ADD COLUMN error VARCHAR(512)"))

        rep_cols = {row[1] for row in conn.execute(text("PRAGMA table_info(reports)")).fetchall()}
        if "classify_source" not in rep_cols:
            conn.execute(text("ALTER TABLE reports ADD COLUMN classify_source VARCHAR(64)"))

    # Backfill source_mailbox for rows written before the column existed
    from app.models import EmailRecord as _ER
    from app.models import infer_source_mailbox as _infer

    with session_factory() as s:
        null_rows = s.query(_ER).filter(_ER.source_mailbox.is_(None)).all()
        for r in null_rows:
            r.source_mailbox = _infer(r.email_id, r.sender)
        if null_rows:
            s.commit()

    # Every report predating classify_source was produced by the rule engine.
    from app.models import ReportRecord as _RR

    with session_factory() as s:
        null_reps = s.query(_RR).filter(_RR.classify_source.is_(None)).all()
        for r in null_reps:
            r.classify_source = "rule-classifier"
        if null_reps:
            s.commit()


def get_db():
    """FastAPI dependency yielding an Enterprise Hub DB scoped session."""
    db = EnterpriseSessionLocal()
    try:
        yield db
    finally:
        db.close()


def get_enterprise_db():
    """Explicit FastAPI dependency for Enterprise Hub database."""
    db = EnterpriseSessionLocal()
    try:
        yield db
    finally:
        db.close()


def get_oauth_db():
    """FastAPI dependency yielding an isolated OAuth DB scoped session."""
    db = OAuthSessionLocal()
    try:
        yield db
    finally:
        db.close()

