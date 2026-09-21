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


# Authoritative source mailboxes in the Enterprise Hub multi-inbox topology.
BUNDLE_MAILBOX = "sdoc-hackathon-bundle@averis.com"
OPS_MAILBOX = "operations@shipsync.demo"


def infer_source_mailbox(email_id: str, sender: str | None = None) -> str:
    """Best-effort origin mailbox for an ingested email.

    The email_id prefix is authoritative (the benchmark bundle and the live
    Gmail/operations stream use stable prefixes); sender is a fallback only.
    """
    eid = email_id or ""
    if eid.startswith("email_") or eid.startswith("520"):
        return BUNDLE_MAILBOX
    if eid.startswith("GMAIL"):
        return OPS_MAILBOX
    if sender and "ops" in sender.lower():
        return OPS_MAILBOX
    return OPS_MAILBOX


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
    source_mailbox = Column(String(128), index=True, nullable=True)  # origin inbox
    disposition_override = Column(String(16), default="INHERIT")  # INHERIT | AUTO | MANUAL


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
    made concrete: ``POST /api/process`` writes here, and both the ShipMail
    side panel and the Dashboard read from here. Nothing is
    re-classified, re-OCR'd, re-extracted or re-verified just because a UI was
    opened, refreshed, or switched between emails.

    It is deliberately kept separate from ``reports`` so ShipMail /
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
    source = Column(String(32), default="shipmail")
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


class ShipmailAssignmentRecord(Base):
    """Triage decision for one email in the ShipMail (OAuth) workspace.

    The hub distributes mail per source mailbox; the operator inbox has a single
    source, so the decision is per email instead. ``AUTO`` means the message may
    be cleared automatically, ``HOLD`` parks it for a human verdict and
    ``IGNORE`` takes it out of the actionable queue. Stored in the OAuth
    database: this is operator mail, not the hub corpus.
    """
    __tablename__ = "shipmail_assignments"

    id = Column(Integer, primary_key=True, autoincrement=True)
    email_id = Column(String(128), unique=True, index=True, nullable=False)
    assignment = Column(String(16), default="HOLD")  # AUTO | HOLD | IGNORE
    note = Column(String(255), nullable=True)
    created_at = Column(DateTime, default=utcnow)
    updated_at = Column(DateTime, default=utcnow, onupdate=utcnow)


class StagedEmailRecord(Base):
    """Emails held in the pre-ingestion staging buffer / quarantine gate."""
    __tablename__ = "staged_emails"

    id = Column(Integer, primary_key=True, autoincrement=True)
    stage_id = Column(String(64), unique=True, index=True, nullable=False)
    source_mailbox = Column(String(128), index=True, default="docs.export@averis.com")
    sender = Column(String(255), nullable=True)
    recipient = Column(String(255), nullable=True)
    subject = Column(String(512), nullable=True)
    body = Column(Text, nullable=True)
    attachments = Column(JSON, default=list)  # list of filenames / descriptors
    security_status = Column(String(32), default="CLEAN")  # CLEAN | BLOCKED | SUSPICIOUS
    security_details = Column(JSON, default=list)  # details per attachment
    category = Column(String(32), default="GENERAL")
    confidence = Column(Float, default=1.0)
    ai_reason = Column(Text, nullable=True)
    status = Column(String(32), default="STAGED")  # STAGED | AUTO_INGESTED | APPROVED | QUARANTINED | REJECTED
    ai_engine = Column(String(64), default="cascade")
    received_at = Column(DateTime, default=utcnow)
    created_at = Column(DateTime, default=utcnow)
    updated_at = Column(DateTime, default=utcnow, onupdate=utcnow)


class GatewayPolicyRecord(Base):
    """Runtime configuration policy for the enterprise IDP hub.

    ``ingest_mode`` is the global default ingest behaviour. ``source_policies``
    is an opt-in override map ``{mailbox: "auto" | "manual"}`` so a high-trust
    source (the benchmark bundle) can auto-ingest while a live operations
    stream stays on strict manual triage. A mailbox absent from the map
    inherits ``ingest_mode``.
    """
    __tablename__ = "gateway_policy"

    id = Column(Integer, primary_key=True, autoincrement=True)
    engine = Column(String(32), default="cascade")  # "cascade" | "ollama" | "rule"
    ingest_mode = Column(String(32), default="auto")  # "auto" | "manual"
    source_policies = Column(JSON, default=dict)  # {mailbox: "auto" | "manual"}
    # Response/disposition policy (post-classification, the outstream buffer):
    # "auto" sends the SIMULATED reply, "manual" parks the email for a human.
    # Resolution mirrors source_policies: a per-source override map over a
    # global default. This is deliberately separate from ingest_mode, which
    # governs the *classification* intake (the instream buffer).
    disposition_mode = Column(String(32), default="manual")  # "auto" | "manual"
    disposition_policies = Column(JSON, default=dict)  # {mailbox: "auto" | "manual"}
    updated_at = Column(DateTime, default=utcnow, onupdate=utcnow)


class DispatchRecord(Base):
    """Audit trail of every outbound return-to-sender dispatched by the hub.

    When an operator finishes triaging a staged email, the hub returns the
    outcome (verification result or rejection notice) through the *same*
    mailbox the transmission arrived on. Each such return is persisted here so
    the round-trip is auditable even though the demo mailboxes have no live
    SMTP server behind them (those rows are flagged ``delivery="SIMULATED"``).
    """
    __tablename__ = "dispatched_emails"

    id = Column(Integer, primary_key=True, autoincrement=True)
    stage_id = Column(String(64), index=True, nullable=False)
    email_id = Column(String(128), index=True, nullable=True)  # link back to the replied email
    source_mailbox = Column(String(128), index=True, nullable=False)  # return path
    recipient = Column(String(255), nullable=True)  # original sender
    decision = Column(String(32), nullable=False)  # VERIFIED | REJECTED
    subject = Column(String(512), nullable=True)
    body = Column(Text, nullable=True)
    channel = Column(String(128), nullable=True)  # "sdoc-hackathon-bundle@averis.com" etc.
    delivery = Column(String(32), default="SIMULATED")  # SIMULATED | SENT | FAILED
    gmail_message_id = Column(String(128), nullable=True)
    error = Column(String(512), nullable=True)  # transport failure reason, when delivery = FAILED
    created_at = Column(DateTime, default=utcnow, onupdate=utcnow)


class OutboundApprovalRecord(Base):
    """Approval of an exact outbound snapshot; a token can be used only once."""
    __tablename__ = "outbound_approvals"
    id = Column(String(64), primary_key=True)
    email_id = Column(String(128), index=True, nullable=False)
    fingerprint = Column(String(64), nullable=False)
    recipient = Column(String(255), nullable=False)
    subject = Column(Text, nullable=False)
    body = Column(Text, nullable=False)
    reviewer = Column(String(128), nullable=False)
    status = Column(String(32), default="APPROVED", nullable=False)
    dispatch_id = Column(Integer, nullable=True)
    created_at = Column(DateTime, default=utcnow, nullable=False)
