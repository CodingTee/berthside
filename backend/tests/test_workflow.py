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
from app.services import workflow


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
}

ATTACHMENTS = {
    "attachments/e_SI.txt": SI_TEXT.encode(),
    "attachments/e_BL.txt": BL_TEXT.encode(),
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
