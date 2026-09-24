"""Tests for the Enterprise Hub source trust policy and bulk operations.

The hub lets an operator pin a per-mailbox ingest mode (auto|manual) that
overrides the global default, bulk-approve or bulk-return by source, and
re-apply the policy to the existing STAGED backlog. Malware-blocked rows must
never be approved by a bulk action or by policy re-application.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.database import get_db
from app.main import app
from app.models import (
    DispatchRecord,
    GatewayPolicyRecord,
    StagedEmailRecord,
)


@pytest.fixture
def client():
    return TestClient(app)


def _stage(db, stage_id, mailbox, status="STAGED", security="CLEAN"):
    rec = StagedEmailRecord(
        stage_id=stage_id,
        source_mailbox=mailbox,
        sender="shipper@fastocean.com",
        recipient=mailbox,
        subject="Booking BKG-8812 - Draft B/L",
        body="Please verify the attached draft B/L.",
        attachments=["Draft_BL.pdf"],
        security_status=security,
        security_details=[],
        category="BL_COMPARISON",
        confidence=1.0,
        ai_reason="ok",
        status=status,
        ai_engine="cloud-cascade (gemini-2.5-flash)",
    )
    db.add(rec)
    db.commit()
    return rec


def test_config_accepts_source_policies(client, db_session):
    res = client.post(
        "/api/v1/gateway/config",
        json={"source_policies": {"operations@berthside.demo": "manual"}},
    )
    assert res.status_code == 200, res.text
    d = res.json()
    assert d["source_policies"] == {"operations@berthside.demo": "manual"}

    # Status reflects the override.
    st = client.get("/api/v1/gateway/status").json()
    assert st["policy"]["source_policies"] == {"operations@berthside.demo": "manual"}


def test_source_policies_persisted_and_queryable(client, db_session):
    client.post(
        "/api/v1/gateway/config",
        json={"source_policies": {"docs.export@averis.com": "auto", "finance@averis.com": "manual"}},
    )
    pol = db_session.query(GatewayPolicyRecord).first()
    assert pol.source_policies == {
        "docs.export@averis.com": "auto",
        "finance@averis.com": "manual",
    }


def test_simulate_drop_honours_per_source_policy(client, db_session):
    # Force the demo mailbox to manual so its clean mail stays STAGED.
    client.post(
        "/api/v1/gateway/config",
        json={"source_policies": {"docs.export@averis.com": "manual"}},
    )
    res = client.post(
        "/api/v1/gateway/simulate-drop",
        json={"scenario": "clean_bl", "source_mailbox": "docs.export@averis.com"},
    )
    assert res.status_code == 200, res.text
    assert res.json()["status"] == "STAGED"

    # A non-overridden mailbox inherits global auto (default) and auto-ingests.
    res2 = client.post(
        "/api/v1/gateway/simulate-drop",
        json={"scenario": "clean_bl", "source_mailbox": "april.shipping@averis.com"},
    )
    assert res2.json()["status"] == "AUTO_INGESTED"


def test_bulk_approve_by_source_skips_blocked(client, db_session):
    _stage(db_session, "STG-A1", "operations@berthside.demo", status="STAGED")
    _stage(db_session, "STG-A2", "operations@berthside.demo", status="STAGED")
    _stage(db_session, "STG-A3", "operations@berthside.demo", status="STAGED", security="BLOCKED")
    _stage(db_session, "STG-B1", "finance@averis.com", status="STAGED")

    res = client.post(
        "/api/v1/gateway/emails/bulk-approve",
        json={"source_mailbox": "operations@berthside.demo"},
    )
    assert res.status_code == 200, res.text
    d = res.json()
    assert d["approved"] == 2
    assert d["skipped_blocked"] == 1

    # The other source is untouched.
    assert (
        db_session.query(StagedEmailRecord)
        .filter_by(stage_id="STG-B1").first().status == "STAGED"
    )
    # Blocked row is still not approved.
    assert (
        db_session.query(StagedEmailRecord)
        .filter_by(stage_id="STG-A3").first().status == "STAGED"
    )


def test_bulk_approve_all_sources(client, db_session):
    _stage(db_session, "STG-C1", "operations@berthside.demo", status="STAGED")
    _stage(db_session, "STG-C2", "finance@averis.com", status="STAGED")
    res = client.post(
        "/api/v1/gateway/emails/bulk-approve",
        json={"source_mailbox": "ALL"},
    )
    assert res.status_code == 200, res.text
    assert res.json()["approved"] == 2


def test_bulk_return_by_source(client, db_session):
    _stage(db_session, "STG-D1", "operations@berthside.demo", status="STAGED")
    _stage(db_session, "STG-D2", "operations@berthside.demo", status="APPROVED")

    res = client.post(
        "/api/v1/gateway/emails/bulk-return",
        json={"source_mailbox": "operations@berthside.demo"},
    )
    assert res.status_code == 200, res.text
    assert res.json()["returned"] == 2
    assert db_session.query(DispatchRecord).count() == 2
    assert (
        db_session.query(StagedEmailRecord)
        .filter_by(stage_id="STG-D1").first().status == "RETURNED"
    )


def test_reapply_policy_auto_ingests_staged_of_auto_sources(client, db_session):
    # Global default is auto, so STAGED rows of non-overridden sources ingest.
    _stage(db_session, "STG-E1", "operations@berthside.demo", status="STAGED")
    _stage(db_session, "STG-E2", "booking@averis.com", status="STAGED")
    # Force one source manual: its STAGED row must stay put.
    client.post(
        "/api/v1/gateway/config",
        json={"source_policies": {"finance@averis.com": "manual"}},
    )
    _stage(db_session, "STG-E3", "finance@averis.com", status="STAGED")

    res = client.post("/api/v1/gateway/policy/reapply")
    assert res.status_code == 200, res.text
    d = res.json()
    assert d["auto_ingested"] == 2  # E1 + E2, not E3

    assert (
        db_session.query(StagedEmailRecord).filter_by(stage_id="STG-E3").first().status
        == "STAGED"
    )
