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


def get_db():
    """FastAPI dependency yielding a scoped session."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
