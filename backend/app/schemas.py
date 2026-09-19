"""Pydantic schemas — the API contract for P1 (frontend) and P3 (AI).

Canonical JSON shapes (also used for the self-evaluation submission).
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

# ---------------------------------------------------------------- constants
EmailCategory = Literal[
    "BL_COMPARISON", "SI_REQUEST", "INVOICE_QUERY", "GENERAL", "SPAM"
]
ReportStatus = Literal[
    "OK", "MISMATCH", "NEEDS_REVIEW", "SKIPPED", "ERROR"
]
ReviewReason = Literal[
    "wrong_doc_type", "missing_attachment", "unreadable",
    "missing_value", "low_confidence",
]
DocType = Literal["SI", "BL"]

COMPARED_FIELDS = [
    "shipper",
    "consignee",
    "notify_party",
    "port_of_loading",
    "port_of_discharge",
    "container_count",
    "gross_weight_kg",
]


# ------------------------------------------------------------------ emails
class EmailOut(BaseModel):
    email_id: str
    sender: str = Field(alias="from")
    subject: str
    body: str
    attachments: list[str] = []
    has_attachments: bool = False

    model_config = ConfigDict(populate_by_name=True)


class EmailListItem(BaseModel):
    """Lightweight row for GET /emails listing."""
    email_id: str
    sender: str = Field(alias="from")
    subject: str
    attachments: list[str] = []
    has_attachments: bool = False
    processed: bool = False
    status: Optional[str] = None

    model_config = ConfigDict(populate_by_name=True)


class EmailListOut(BaseModel):
    total: int
    limit: int
    offset: int
    emails: list[EmailListItem]


# ----------------------------------------------------------------- reports
class FieldResult(BaseModel):
    field: str
    si_value: Any = None
    bl_value: Any = None
    match: Optional[bool] = None  # None = could not decide


class ReportOut(BaseModel):
    """Full processing report for one email."""
    # ORM rows expose the primary key as `id`; the API calls it `report_id`.
    report_id: int = Field(alias="id")
    email_id: str
    category: EmailCategory
    status: ReportStatus
    has_defect: bool = False
    defect_fields: list[str] = []
    review_reason: Optional[str] = None
    field_results: list[FieldResult] = []
    extracted: dict[str, Any] = {}
    error_message: Optional[str] = None
    attempts: int = 0
    reviewed: bool = False
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    model_config = ConfigDict(from_attributes=True, populate_by_name=True)


class ReportSummary(BaseModel):
    report_id: int
    email_id: str
    category: EmailCategory
    status: ReportStatus
    has_defect: bool = False
    defect_fields: list[str] = []
    review_reason: Optional[str] = None

    model_config = ConfigDict(from_attributes=True)


class ReportListOut(BaseModel):
    total: int
    limit: int
    offset: int
    reports: list[ReportSummary]


class ProcessResponse(BaseModel):
    email_id: str
    category: EmailCategory
    status: ReportStatus
    has_defect: bool = False
    defect_fields: list[str] = []
    review_reason: Optional[str] = None
    field_results: list[FieldResult] = []
    error_message: Optional[str] = None
    attempts: int = 0
    processing_ms: Optional[float] = None


class ProcessAllResponse(BaseModel):
    requested: int
    processed: int
    succeeded: int
    failed: int
    by_status: dict[str, int] = {}
    by_category: dict[str, int] = {}
    elapsed_ms: float


# ----------------------------------------------------------------- reviews
class ReviewCreate(BaseModel):
    decision: Literal["CONFIRM", "CORRECT", "REJECT"]
    corrected_fields: dict[str, Any] = {}
    corrected_category: Optional[EmailCategory] = None
    reviewer: str = "human"
    notes: Optional[str] = None


class ReviewOut(BaseModel):
    id: int
    report_id: int
    email_id: str
    reviewer: str
    decision: str
    corrected_fields: dict[str, Any] = {}
    corrected_category: Optional[str] = None
    notes: Optional[str] = None
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


# ------------------------------------------------------------- submission
class SubmissionEntry(BaseModel):
    """Exactly the shape of sample_submission.json values."""
    category: EmailCategory
    status: ReportStatus
    review_reason: Optional[str] = None
    has_defect: bool = False
    defect_fields: list[str] = []


class SubmissionOut(BaseModel):
    """Dict keyed by email_id — the self-evaluation submission."""
    submission: dict[str, SubmissionEntry]
    emails_covered: int
    total_inbox: int


# ------------------------------------------------------------------- misc
class HealthOut(BaseModel):
    status: str
    app_name: str
    version: str
    environment: str
    database: str
    data_source: str
    ai_provider: str
    emails_cached: int = 0
    reports_stored: int = 0


# ------------------------------------------------------------- shipments
class DocumentVersionOut(BaseModel):
    id: int
    shipment_id: int
    document_id: int
    doc_type: str
    filename: str
    email_id: Optional[str] = None
    version_number: int
    previous_version_id: Optional[int] = None
    duplicate_of_version_id: Optional[int] = None
    is_latest: bool = False
    document_status: str
    extracted_fields: dict[str, Any] = Field(default_factory=dict)
    received_at: Optional[datetime] = None
    created_at: Optional[datetime] = None

    model_config = ConfigDict(from_attributes=True)


class DocumentOut(BaseModel):
    id: int
    shipment_id: int
    doc_type: str
    document_key: str
    versions: list[DocumentVersionOut] = Field(default_factory=list)

    model_config = ConfigDict(from_attributes=True)


class ShipmentSummaryOut(BaseModel):
    id: int
    shipment_key: str
    reference_number: Optional[str] = None
    status: str
    si_latest: Optional[DocumentVersionOut] = None
    bl_latest: Optional[DocumentVersionOut] = None
    mismatch_count: int = 0
    pending_count: int = 0
    resolved_count: int = 0

    model_config = ConfigDict(from_attributes=True)


class ShipmentListOut(BaseModel):
    total: int
    shipments: list[ShipmentSummaryOut]


class ShipmentDetailOut(ShipmentSummaryOut):
    documents: list[DocumentOut] = Field(default_factory=list)


class VersionFieldDiff(BaseModel):
    field: str
    from_value: Any = None
    to_value: Any = None
    changed: bool


class VersionDiffOut(BaseModel):
    shipment_id: int
    from_version_id: int
    to_version_id: int
    fields: list[VersionFieldDiff]


# ------------------------------------------------------------- resolution
class IssueOut(BaseModel):
    id: int
    issue_key: str
    shipment_id: int
    report_id: int
    email_id: str
    field_name: str
    si_value: Any = None
    bl_value: Any = None
    difference: Optional[str] = None
    explanation: str
    status: str
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    model_config = ConfigDict(from_attributes=True)


class ResolutionOut(BaseModel):
    id: int
    issue_id: int
    shipment_id: int
    report_id: int
    field_name: str
    original_si_value: Any = None
    original_bl_value: Any = None
    suggested_value: Any = None
    suggested_action: str
    status: str
    reviewed_at: Optional[datetime] = None
    reviewed_by: Optional[str] = None
    review_comment: Optional[str] = None
    corrected_draft_path: Optional[str] = None
    created_at: Optional[datetime] = None

    model_config = ConfigDict(from_attributes=True)


class IssueDetailOut(IssueOut):
    resolution: Optional[ResolutionOut] = None


class ResolutionReviewIn(BaseModel):
    reviewed_by: str = "human"
    review_comment: Optional[str] = None


class ResolutionStatusOut(BaseModel):
    shipment_id: int
    status: str
    total_issues: int
    resolved: int
    pending_approval: int
    rejected: int
    open: int


# ------------------------------------------------------------- AI assist
class AIMismatchAssistOut(BaseModel):
    issue_id: int
    field_name: str
    provider: str
    confidence: str
    explanation: str
    suggestion: str
    safety_note: str
    source_values: dict[str, Any] = Field(default_factory=dict)


class AIEmailDraftOut(BaseModel):
    shipment_id: int
    provider: str
    confidence: str
    subject: str
    body: str
    requires_human_review: bool = True


class AIAmbiguousInterpretationIn(BaseModel):
    text: str
    field_name: Optional[str] = None


class AIAmbiguousInterpretationOut(BaseModel):
    provider: str
    input_text: str
    field_name: Optional[str] = None
    interpreted_value: Optional[str] = None
    confidence: str
    needs_human_review: bool = True
    explanation: str
