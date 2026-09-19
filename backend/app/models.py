"""SQLAlchemy ORM models.

Tables
------
emails    : cached copy of every inbox record we have seen (audit trail)
reports   : one processing result per email (latest wins — keyed by email_id)
reviews   : human-in-the-loop decisions attached to a report
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import JSON, Column, DateTime, Float, Integer, String, Text

from app.database import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class EmailRecord(Base):
    __tablename__ = "emails"

    id = Column(Integer, primary_key=True, autoincrement=True)
    email_id = Column(String(64), unique=True, index=True, nullable=False)
    sender = Column(String(255))
    subject = Column(String(512))
    body = Column(Text)
    attachments = Column(JSON, default=list)  # list[str]
    received_at = Column(DateTime, nullable=True)
    first_seen_at = Column(DateTime, default=utcnow)


class ReportRecord(Base):
    __tablename__ = "reports"

    id = Column(Integer, primary_key=True, autoincrement=True)
    email_id = Column(String(64), unique=True, index=True, nullable=False)

    # classification
    category = Column(String(32))  # BL_COMPARISON | SI_REQUEST | INVOICE_QUERY | GENERAL | SPAM

    # outcome for BL_COMPARISON emails
    status = Column(String(32))   # OK | MISMATCH | NEEDS_REVIEW | SKIPPED | ERROR
    has_defect = Column(Integer, default=0)  # 0/1 boolean
    defect_fields = Column(JSON, default=list)  # list[str]
    review_reason = Column(String(64), nullable=True)
    # wrong_doc_type | missing_attachment | unreadable | missing_value | low_confidence

    # evidence — per-field comparison detail
    field_results = Column(JSON, default=list)
    # [{field, si_value, bl_value, match: bool}, ...]
    extracted = Column(JSON, default=dict)  # {si: {...}, bl: {...}}
    error_message = Column(Text, nullable=True)

    # bookkeeping
    attempts = Column(Integer, default=0)
    processing_ms = Column(Float, nullable=True)
    reviewed = Column(Integer, default=0)  # 0/1 — set when a review lands
    created_at = Column(DateTime, default=utcnow)
    updated_at = Column(DateTime, default=utcnow, onupdate=utcnow)


class ReviewRecord(Base):
    __tablename__ = "reviews"

    id = Column(Integer, primary_key=True, autoincrement=True)
    report_id = Column(Integer, index=True, nullable=False)
    email_id = Column(String(64), index=True, nullable=False)
    reviewer = Column(String(128), default="human")
    decision = Column(String(32), nullable=False)  # CONFIRM | CORRECT | REJECT
    corrected_fields = Column(JSON, default=dict)  # {field: final_value, ...}
    corrected_category = Column(String(32), nullable=True)
    notes = Column(Text, nullable=True)
    created_at = Column(DateTime, default=utcnow)
