"""Workflow / integration tests.

Covers classify → extract → compare → persist → review, plus the failure and
retry paths, using an isolated in-memory database and a stubbed inbox.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import models  # noqa: F401  registers tables
from app.database import Base
from app.services import ai_enhancement, workflow


# --------------------------------------------------------------------- fixture
SI_TEXT = """SHIPPING INSTRUCTION
Shipper: APRIL FAR EAST (M) SDN BHD
Consignee: EAST BRIGHT FZ-LLC
Notify: EAST BRIGHT FZ-LLC
Port of Loading (POL): NANTONG, CHINA (CNNTG)
POD: KARACHI, PAKISTAN (PKKHI)
Total Containers: 6 x 40'HC
Gross Wt (kgs): 131,058 KG
"""

BL_TEXT = """BILL OF LADING (DRAFT)
SHIPPER: APRIL FAR EAST (M) SDN BHD
To the Order of: UAB NOVAKOPA
Notify Party: UAB NOVAKOPA
Load Port: NANTONG, CHINA (CNNTG)
Port of Discharge: KARACHI, PAKISTAN (PKKHI)
Container Count: 6 x 40'HC
Gross Weight (KG): 131,058 KG
"""

BL_TEXT_MATCHING_LATEST = """BILL OF LADING (DRAFT)
SHIPPER: APRIL FAR EAST (M) SDN BHD
To the Order of: EAST BRIGHT FZ-LLC
Notify Party: EAST BRIGHT FZ-LLC
Load Port: NANTONG, CHINA (CNNTG)
Port of Discharge: KARACHI, PAKISTAN (PKKHI)
Container Count: 6 x 40'HC
Gross Weight (KG): 131,058 KG
"""

INBOX = {
    "email_900": {
        "email_id": "email_900", "from": "docs@co.com",
        "subject": "REQUEST BL DRAFT - please check",
        "body": "Attached are the SI and draft BL. Please check.",
        "attachments": ["attachments/e_SI.txt", "attachments/e_BL.txt"],
    },
    "email_901": {
        "email_id": "email_901", "from": "sales@co.com",
        "subject": "REQUEST SI _ 5RFR-37631",
        "body": "Please issue the shipping instruction.",
        "attachments": [],
    },
    "email_902": {
        "email_id": "email_902", "from": "info@crypto-invest.net",
        "subject": "Increase your shipping revenue with this ONE weird trick",
        "body": "Click here to win a prize. Unsubscribe anytime.",
        "attachments": [],
    },
    "email_903": {
        "email_id": "email_903", "from": "finance@co.com",
        "subject": "Outstanding invoice charges",
        "body": "Please settle the outstanding payment.",
        "attachments": [],
    },
    "email_904": {  # comparison request with no attachments
        "email_id": "email_904", "from": "ops@co.com",
        "subject": "TO CONFIRM DOCS _ 5SUS-42284",
        "body": "Please check the documents.",
        "attachments": [],
    },
    "email_905": {
        "email_id": "email_905", "from": "docs@co.com",
        "subject": "REQUEST BL DRAFT - updated SDOC-900",
        "body": "Attached are the SI and updated BL. Please check.",
        "attachments": ["attachments/e2_SI.txt", "attachments/e2_BL.txt"],
    },
    "email_906": {
        "email_id": "email_906", "from": "docs@co.com",
        "subject": "REQUEST BL DRAFT - duplicate SDOC-900",
        "body": "Same attachment again.",
        "attachments": ["attachments/e2_SI.txt", "attachments/e2_BL.txt"],
    },
    "email_907": {  # the sender asks US to send the draft BL — nothing to verify
        "email_id": "email_907", "from": "exports@co.com",
        "subject": "RE_ TO CONFIRM DOCS _ 5AAT-03056",
        "body": ("Dear Hari, Please assist to send the draft BL for SIN832764835 "
                 "for checking asap. Thank you."),
        "attachments": [],
    },
    "email_908": {  # asks us to compare, and says the documents never arrived
        "email_id": "email_908", "from": "ops@co.com",
        "subject": "AFRT - LONG BEACH_US - 5RSG-19787",
        "body": ("Dear Team, Please compare the SI and draft BL for 070500263211 "
                 "and confirm (attachments appear to have been dropped). Thank you."),
        "attachments": [],
    },
    "email_909": {  # both documents present, named the way senders really name them
        "email_id": "email_909", "from": "docs@co.com",
        "subject": "REQUEST BL DRAFT - please check",
        "body": "Attached are the SI and draft BL. Please check.",
        "attachments": ["attachments/Shipping_Instruction_4471.txt",
                        "attachments/Draft BL v2.txt"],
    },
}

ATTACHMENTS = {
    "attachments/e_SI.txt": SI_TEXT.encode(),
    "attachments/e_BL.txt": BL_TEXT.encode(),
    "attachments/e2_SI.txt": SI_TEXT.encode(),
    "attachments/e2_BL.txt": BL_TEXT_MATCHING_LATEST.encode(),
    "attachments/Shipping_Instruction_4471.txt": SI_TEXT.encode(),
    "attachments/Draft BL v2.txt": BL_TEXT.encode(),
}


@pytest.fixture()
def db(monkeypatch):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()

    monkeypatch.setattr(workflow.inbox_service, "all_emails",
                        lambda: list(INBOX.values()))
    monkeypatch.setattr(workflow.inbox_service, "get_email",
                        lambda eid: INBOX.get(eid))
    monkeypatch.setattr(workflow.inbox_service, "read_attachment",
                        lambda p: ATTACHMENTS[p])
    yield session
    session.close()


# ----------------------------------------------------------------------- tests
def test_comparison_request_produces_mismatch(db):
    r = workflow.process_email(db, "email_900")
    assert r.category == "BL_COMPARISON"
    assert r.status == "MISMATCH"
    assert set(r.defect_fields) == {"consignee", "notify_party"}
    assert r.has_defect == 1
    assert r.error_message is None


def test_si_request_is_classification_only(db):
    r = workflow.process_email(db, "email_901")
    assert r.category == "SI_REQUEST"
    assert r.status == "SKIPPED"
    assert r.defect_fields == []


def test_spam_is_detected(db):
    r = workflow.process_email(db, "email_902")
    assert r.category == "SPAM"


def test_invoice_query_is_detected(db):
    assert workflow.process_email(db, "email_903").category == "INVOICE_QUERY"


def test_comparison_request_without_attachments_escalates(db):
    r = workflow.process_email(db, "email_904")
    assert r.status == "NEEDS_REVIEW"
    assert r.review_reason == "missing_attachment"


def test_document_request_without_attachments_is_ok_not_escalated(db):
    """"Please assist to send the draft BL ..." asks us for a document.

    Nothing is missing and nothing can be compared, so escalating would put a
    task in the human queue that does not exist. Measured against the official
    scorer: 91 of the 106 escalations were exactly this, which is what dragged
    escalation precision down to 0.14. Now the rule separates the two families
    with 0 errors on all 94 no-attachment BL_COMPARISON emails.
    """
    r = workflow.process_email(db, "email_907")
    assert r.category == "BL_COMPARISON"
    assert r.status == "OK"
    assert r.review_reason is None
    assert r.defect_fields == []
    assert r.extracted["action"] == "document_request"


def test_compare_request_with_declared_missing_docs_still_escalates(db):
    """The task is ours and we cannot do it — this one keeps its escalation."""
    r = workflow.process_email(db, "email_908")
    assert r.status == "NEEDS_REVIEW"
    assert r.review_reason == "missing_attachment"


# ------------------------------------------- attachment naming: tiers 1 and 2
def test_attachments_named_without_the_convention_are_still_compared(db):
    """Real senders write "Draft BL v2", not "email_123_BL".

    Both files are present under names the convention does not recognise. Before
    tier 2 this reached the reviewer as `missing_attachment`, which blames the
    sender for a file that was in fact attached: our parsing gap reported as
    their mistake.
    """
    r = workflow.process_email(db, "email_909")
    assert r.status == "MISMATCH"
    assert r.review_reason is None
    assert set(r.defect_fields) == {"consignee", "notify_party"}


def test_an_ambiguous_attachment_name_is_bound_to_neither_side():
    """A file naming both types must not become the SI *and* the BL."""
    paths = workflow._find_doc_attachments(
        {"attachments": ["attachments/SI and BL combined.txt"]}
    )
    assert paths == (None, None)


def test_the_convention_outranks_tier_two():
    """Tier 2 only fills gaps; it never overrides a name tier 1 already read.

    "draft_BL_and_SI.txt" ends with "_SI.txt", so tier 1 calls it the SI, while
    tier 2 would see both tokens and refuse to choose. Tier 1 decides.
    """
    paths = workflow._find_doc_attachments(
        {"attachments": ["attachments/draft_BL_and_SI.txt"]}
    )
    assert paths == ("attachments/draft_BL_and_SI.txt", None)


def test_tier_two_never_binds_one_file_to_both_sides():
    """A single attachment claiming both roles is worse than no attachment."""
    paths = workflow._find_doc_attachments(
        {"attachments": ["attachments/Draft_BL_v2.txt", "attachments/notes.txt"]}
    )
    assert paths == (None, "attachments/Draft_BL_v2.txt")


def test_processing_is_idempotent_and_counts_attempts(db):
    first = workflow.process_email(db, "email_900")
    first_id, first_attempts = first.id, first.attempts
    second = workflow.process_email(db, "email_900")
    assert second.id == first_id             # same row, not duplicated
    assert second.attempts == first_attempts + 1
    assert second.status == first.status


def test_batch_process_covers_every_email(db):
    stats = workflow.process_all(db)
    assert stats["requested"] == len(INBOX)
    assert stats["failed"] == 0
    assert sum(stats["by_status"].values()) == len(INBOX)


def test_human_review_updates_the_report(db):
    workflow.process_email(db, "email_900")
    r = workflow.apply_review(db, "email_900", "CORRECT",
                              corrected_fields={"consignee": "EAST BRIGHT FZ-LLC"},
                              corrected_category=None,
                              reviewer="tester", notes="checked by hand")
    assert r.reviewed == 1
    assert "consignee" not in (r.defect_fields or [])


def test_status_reflects_state(db):
    workflow.process_email(db, "email_900")
    s = workflow.get_status(db, "email_900")
    assert s["processed"] is True
    assert s["state"] == "COMPLETED"
    assert s["status"] == "MISMATCH"


def test_unknown_email_status_is_pending(db):
    s = workflow.get_status(db, "email_999")
    assert s["processed"] is False
    assert s["state"] == "PENDING"


def test_pipeline_failure_is_recorded_not_raised(db, monkeypatch):
    healthy = workflow.ai_service.classify_email

    def boom(*_a, **_k):
        raise RuntimeError("classification service exploded")

    monkeypatch.setattr(workflow.ai_service, "classify_email", boom)
    r = workflow.process_email(db, "email_900")
    assert r.status == "ERROR"
    assert "RuntimeError" in r.error_message

    # ...and a retry recovers once the dependency is healthy again.
    monkeypatch.setattr(workflow.ai_service, "classify_email", healthy)
    stats = workflow.retry_failed(db, statuses=["ERROR"])
    assert stats["retried"] >= 1
    assert stats["recovered"] >= 1


def test_latest_bl_version_is_used_for_shipment_comparison(db):
    first = workflow.process_email(db, "email_900")
    assert first.status == "MISMATCH"

    second = workflow.process_email(db, "email_905")
    assert second.status == "OK"
    assert second.defect_fields == []
    assert second.extracted["latest_bl_version_id"] is not None


def test_duplicate_document_does_not_advance_latest_version(db):
    workflow.process_email(db, "email_905")
    first_latest = (
        db.query(models.DocumentVersionRecord)
        .filter_by(doc_type="BL", is_latest=1)
        .first()
    )
    workflow.process_email(db, "email_906")
    latest = (
        db.query(models.DocumentVersionRecord)
        .filter_by(doc_type="BL", is_latest=1)
        .first()
    )
    duplicate = (
        db.query(models.DocumentVersionRecord)
        .filter_by(doc_type="BL", document_status="DUPLICATE")
        .first()
    )
    assert latest.id == first_latest.id
    assert duplicate.version_number == first_latest.version_number
    assert duplicate.is_latest == 0


def test_mismatch_creates_resolution_record(db):
    report = workflow.process_email(db, "email_900")
    issue = db.query(models.IssueRecord).filter_by(report_id=report.id).first()
    resolution = db.query(models.ResolutionRecord).filter_by(issue_id=issue.id).first()
    assert issue.status == "SUGGESTED"
    assert resolution.status == "PENDING_APPROVAL"
    assert resolution.suggested_value == issue.si_value


def test_ai_mismatch_assistance_uses_structured_issue_data(db):
    report = workflow.process_email(db, "email_900")
    issue = db.query(models.IssueRecord).filter_by(report_id=report.id).first()

    assist = ai_enhancement.mismatch_assistance(db, issue.id)

    assert assist["provider"] == "rule"
    assert assist["confidence"] in {"HIGH", "MEDIUM", "LOW"}
    assert str(issue.si_value) in assist["explanation"]
    assert str(issue.bl_value) in assist["explanation"]
    assert "authoritative" in assist["suggestion"]
    assert "human" in assist["safety_note"].lower()


def test_ai_correction_email_requires_human_review(db):
    report = workflow.process_email(db, "email_900")
    shipment_id = report.extracted["shipment_id"]

    draft = ai_enhancement.correction_email_draft(db, shipment_id)

    assert draft["requires_human_review"] is True
    assert "Action Required" in draft["subject"]
    assert "Please review and confirm" in draft["body"]
    assert "SI:" in draft["body"]
    assert "BL:" in draft["body"]


def test_ai_ambiguous_interpretation_is_low_confidence():
    out = ai_enhancement.ambiguous_interpretation("24,5OO KG", "gross_weight_kg")

    assert out["interpreted_value"] == "24,500 KG"
    assert out["confidence"] == "LOW"
    assert out["needs_human_review"] is True
