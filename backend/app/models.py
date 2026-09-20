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


class ShipmentRecord(Base):
    __tablename__ = "shipments"

    id = Column(Integer, primary_key=True, autoincrement=True)
    shipment_key = Column(String(128), unique=True, index=True, nullable=False)
    reference_number = Column(String(128), nullable=True)
    status = Column(String(32), default="NEEDS_REVIEW")
    created_at = Column(DateTime, default=utcnow)
    updated_at = Column(DateTime, default=utcnow, onupdate=utcnow)


class DocumentRecord(Base):
    __tablename__ = "documents"

    id = Column(Integer, primary_key=True, autoincrement=True)
    shipment_id = Column(Integer, index=True, nullable=False)
    doc_type = Column(String(16), index=True, nullable=False)  # SI | BL
    document_key = Column(String(160), index=True, nullable=False)
    created_at = Column(DateTime, default=utcnow)
    updated_at = Column(DateTime, default=utcnow, onupdate=utcnow)


class DocumentVersionRecord(Base):
    __tablename__ = "document_versions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    shipment_id = Column(Integer, index=True, nullable=False)
    document_id = Column(Integer, index=True, nullable=False)
    doc_type = Column(String(16), index=True, nullable=False)
    filename = Column(String(512), nullable=False)
    email_id = Column(String(64), index=True, nullable=True)
    version_number = Column(Integer, nullable=False)
    previous_version_id = Column(Integer, nullable=True)
    duplicate_of_version_id = Column(Integer, nullable=True)
    is_latest = Column(Integer, default=0)
    document_status = Column(String(32), default="ACTIVE")
    content_hash = Column(String(128), index=True, nullable=False)
    normalized_hash = Column(String(128), index=True, nullable=False)
    extracted_fields = Column(JSON, default=dict)
    raw_text_preview = Column(Text, nullable=True)
    received_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=utcnow)


class IssueRecord(Base):
    __tablename__ = "issues"

    id = Column(Integer, primary_key=True, autoincrement=True)
    issue_key = Column(String(160), unique=True, index=True, nullable=False)
    shipment_id = Column(Integer, index=True, nullable=False)
    report_id = Column(Integer, index=True, nullable=False)
    email_id = Column(String(64), index=True, nullable=False)
    field_name = Column(String(64), index=True, nullable=False)
    si_value = Column(JSON, nullable=True)
    bl_value = Column(JSON, nullable=True)
    difference = Column(String(128), nullable=True)
    explanation = Column(Text, nullable=False)
    status = Column(String(32), default="OPEN")
    created_at = Column(DateTime, default=utcnow)
    updated_at = Column(DateTime, default=utcnow, onupdate=utcnow)


class ResolutionRecord(Base):
    __tablename__ = "resolutions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    issue_id = Column(Integer, unique=True, index=True, nullable=False)
    shipment_id = Column(Integer, index=True, nullable=False)
    report_id = Column(Integer, index=True, nullable=False)
    field_name = Column(String(64), index=True, nullable=False)
    original_si_value = Column(JSON, nullable=True)
    original_bl_value = Column(JSON, nullable=True)
    suggested_value = Column(JSON, nullable=True)
    suggested_action = Column(String(128), nullable=False)
    status = Column(String(32), default="SUGGESTED")
    reviewed_at = Column(DateTime, nullable=True)
    reviewed_by = Column(String(128), nullable=True)
    review_comment = Column(Text, nullable=True)
    corrected_draft_path = Column(String(512), nullable=True)
    created_at = Column(DateTime, default=utcnow)
    updated_at = Column(DateTime, default=utcnow, onupdate=utcnow)


class ProcessResultRecord(Base):
    """Cache of one processing result per email / message / thread.

    This table is the "process once, store the result, reuse the result" rule
    made concrete: ``POST /api/process`` writes here, and both the Simulated
    Gmail side panel and the Dashboard read from here. Nothing is
    re-classified, re-OCR'd, re-extracted or re-verified just because a UI was
    opened, refreshed, or switched between emails.

    It is deliberately kept separate from ``reports`` so simulated-Gmail /
    real-Gmail traffic never pollutes the scored hackathon corpus.
    """

    __tablename__ = "process_results"

    id = Column(Integer, primary_key=True, autoincrement=True)
    # Identity of what was processed — email_id / message_id / thread_id are
    # all indexed so any of them can serve as the dedup key.
    cache_key = Column(String(160), unique=True, index=True, nullable=False)
    email_id = Column(String(128), index=True, nullable=True)
    message_id = Column(String(160), index=True, nullable=True)
    thread_id = Column(String(160), index=True, nullable=True)
    shipment_id = Column(String(64), index=True, nullable=True)
    source = Column(String(32), default="simulated-gmail")
    # Fingerprint of the inputs. If the email or its attachments actually
    # change, reprocessing is legitimate and the entry is refreshed instead
    # of being blindly reused.
    content_hash = Column(String(64), index=True, nullable=True)

    # Per-document fingerprints {doc_type: sha1} for this run. Counting the
    # distinct fingerprints seen for a (shipment, doc_type) pair is what gives
    # the side panel its "Shipping Instruction v3" version numbers.
    doc_fingerprints = Column(JSON, default=dict)

    # Stored processing result — exactly the JSON the API returned.
    result = Column(JSON, nullable=False)
    attempts = Column(Integer, default=0)
    processing_ms = Column(Float, nullable=True)

    processed_at = Column(DateTime, default=utcnow)
    updated_at = Column(DateTime, default=utcnow, onupdate=utcnow)


class GmailMessageRecord(Base):
    __tablename__ = "gmail_messages"

    id = Column(Integer, primary_key=True, autoincrement=True)
    gmail_message_id = Column(String(128), unique=True, index=True, nullable=False)
    thread_id = Column(String(128), index=True, nullable=True)
    history_id = Column(String(128), nullable=True)
    email_id = Column(String(128), unique=True, index=True, nullable=False)
    sender = Column(String(255), nullable=True)
    subject = Column(String(512), nullable=True)
    processed_at = Column(DateTime, nullable=True)
    processing_status = Column(String(32), default="PENDING")
    error_message = Column(Text, nullable=True)
    created_at = Column(DateTime, default=utcnow)
    updated_at = Column(DateTime, default=utcnow, onupdate=utcnow)
