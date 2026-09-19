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
