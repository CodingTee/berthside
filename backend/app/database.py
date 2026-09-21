"""Database engine + session factory.

Works with SQLite (local dev) and PostgreSQL/Supabase (production) — the only
difference is DATABASE_URL in .env.
"""
from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.orm import declarative_base, sessionmaker

from app.config import get_settings

settings = get_settings()

connect_args = {}
if settings.database_url.startswith("sqlite"):
    # Allow the session to be used across request threads.
    connect_args = {"check_same_thread": False}

engine = create_engine(
    settings.database_url,
    connect_args=connect_args,
    pool_pre_ping=True,
)

SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)

Base = declarative_base()


def init_db() -> None:
    """Create tables if they do not exist yet (idempotent)."""
    import app.models  # noqa: F401  (register models on Base)
    Base.metadata.create_all(bind=engine)
    _migrate_columns()


def _migrate_columns() -> None:
    """Add columns introduced after the first schema release.

    create_all does not alter existing tables, so columns added to live
    databases must be migrated explicitly. Dev/SQLite only; Postgres uses
    proper migrations in production.
    """
    if not settings.database_url.startswith("sqlite"):
        return
    from sqlalchemy import text

    with engine.begin() as conn:
        cols = {row[1] for row in conn.execute(text("PRAGMA table_info(emails)")).fetchall()}
        if "source_mailbox" not in cols:
            conn.execute(text("ALTER TABLE emails ADD COLUMN source_mailbox VARCHAR(128)"))

        gp_cols = {row[1] for row in conn.execute(text("PRAGMA table_info(gateway_policy)")).fetchall()}
        if "source_policies" not in gp_cols:
            conn.execute(text("ALTER TABLE gateway_policy ADD COLUMN source_policies TEXT"))

    # Backfill source_mailbox for rows written before the column existed,
    # keyed on the same email_id prefixes the hub uses elsewhere.
    from app.models import EmailRecord as _ER
    from app.models import infer_source_mailbox as _infer

    with SessionLocal() as s:
        null_rows = s.query(_ER).filter(_ER.source_mailbox.is_(None)).all()
        for r in null_rows:
            r.source_mailbox = _infer(r.email_id, r.sender)
        if null_rows:
            s.commit()


def get_db():
    """FastAPI dependency yielding a scoped session."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
