"""The file we hand the grader must stay inside the published contract.

`sample_submission.json` and the organisers' `scoring.py` only know three
statuses: OK, MISMATCH, NEEDS_REVIEW. Internally we also carry SKIPPED
(classification only) and ERROR, and those must be mapped before export.
"""
from app.services.submission import submission_entry, submission_status


def test_internal_statuses_are_mapped_into_the_contract():
    assert submission_status("SKIPPED", "INVOICE_QUERY") == "OK"
    assert submission_status("SKIPPED", "SI_REQUEST") == "OK"
    assert submission_status("SKIPPED", "GENERAL") == "OK"
    assert submission_status("SKIPPED", "SPAM") == "OK"
    # a comparison email we could not decide is escalated, never reported clean
    assert submission_status("SKIPPED", "BL_COMPARISON") == "NEEDS_REVIEW"
    assert submission_status("ERROR", "BL_COMPARISON") == "NEEDS_REVIEW"
    assert submission_status(None, "SPAM") == "OK"


def test_contract_statuses_pass_through_unchanged():
    for s in ("OK", "MISMATCH", "NEEDS_REVIEW"):
        assert submission_status(s, "BL_COMPARISON") == s


def test_entry_shape_matches_sample_submission():
    class Report:  # minimal stand-in for ReportRecord
        category = "SI_REQUEST"
        status = "SKIPPED"
        review_reason = None
        has_defect = 0
        defect_fields = None

    assert submission_entry(Report()) == {
        "category": "SI_REQUEST",
        "status": "OK",
        "review_reason": None,
        "has_defect": False,
        "defect_fields": [],
    }
