"""Submission-shape helpers shared by the API and the offline scoring script.

The scoring contract only knows three statuses: `OK`, `MISMATCH` and
`NEEDS_REVIEW` (see `sample_submission.json` and the organisers' `scoring.py`).
Internally the pipeline also stores `SKIPPED` ("classification only, nothing to
compare") and `ERROR`, and those must never reach the file we hand to the
grader, so every builder maps them here instead of passing them through.
"""
from __future__ import annotations

CONTRACT_STATUSES = ("OK", "MISMATCH", "NEEDS_REVIEW")


def submission_status(status: str | None, category: str | None = None) -> str:
    """Map an internal report status onto a value the contract allows."""
    s = (status or "").upper()
    if s == "ERROR":
        return "NEEDS_REVIEW"
    if s in CONTRACT_STATUSES:
        return s
    # SKIPPED or anything unexpected: a non-comparison email is OK (which is
    # exactly what the official gold uses for those), while a comparison email
    # we could not decide is escalated rather than reported clean.
    return "NEEDS_REVIEW" if category == "BL_COMPARISON" else "OK"


def submission_entry(report) -> dict:
    """One email's record in `sample_submission.json` shape."""
    return {
        "category": report.category,
        "status": submission_status(report.status, report.category),
        "review_reason": report.review_reason,
        "has_defect": bool(report.has_defect),
        "defect_fields": report.defect_fields or [],
    }
