"""End-to-end tests for the external ingestion and analyze API.

Covers:
1. Stateless analyze with a matching SI and BL -> OK
2. Stateless analyze with a discrepancy (3 vs 4 containers) -> MISMATCH
3. Stateful ingest -> saved, then visible through the Review Desk endpoint
4. Security interception of a dangerous attachment -> SECURITY_ALERT
5. Routing a non-comparison email (INVOICE_QUERY) -> SKIPPED

These run under `tests/conftest.py`, which points every test at a throwaway
database and a temporary ingest directory. This file previously lived in
`scripts/` with a `__main__` runner and no isolation, so running it wrote an
`EXT-TEST-0099` row into the real `sdoc.db` and two files into
`backend/data/ingested/`. Keep it in `tests/` and drive it with pytest:

    python -m pytest tests/test_ingest_api.py -v -s
"""
from __future__ import annotations

import base64
import sys
from pathlib import Path

import pytest

BACKEND_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND_ROOT))

from fastapi.testclient import TestClient

from app.main import app

SAMPLE_SI_TEXT = """SHIPPING INSTRUCTION
========================================
Shipper: EAST BRIGHT ENTERPRISE CO LTD
Consignee: OCEANIC LOGISTICS GMBH
Notify Party: PACIFIC FREIGHT SERVICES
Port of Loading: NANTONG, CHINA (CNNTG)
Port of Discharge: ROTTERDAM, NETHERLANDS (NLRTM)
Total Containers: 3 x 40'HC
Gross Weight: 22,000 KG
Commodity: COPIER PAPER
Booking No.: BKG-883921
"""

SAMPLE_BL_MATCH_TEXT = """BILL OF LADING (DRAFT)
========================================
Shipper: EAST BRIGHT ENTERPRISE CO LTD
Consignee: OCEANIC LOGISTICS GMBH
Notify Party: PACIFIC FREIGHT SERVICES
Port of Loading: NANTONG, CHINA (CNNTG)
Port of Discharge: ROTTERDAM, NETHERLANDS (NLRTM)
Total Containers: 3 x 40'HC
Gross Weight: 22,000 KG
Vessel: EVER GIVEN V.012
B/L No.: BL-773910
"""

SAMPLE_BL_MISMATCH_TEXT = """BILL OF LADING (DRAFT)
========================================
Shipper: EAST BRIGHT ENTERPRISE CO LTD
Consignee: OCEANIC LOGISTICS GMBH
Notify Party: PACIFIC FREIGHT SERVICES
Port of Loading: NANTONG, CHINA (CNNTG)
Port of Discharge: ROTTERDAM, NETHERLANDS (NLRTM)
Total Containers: 4 x 40'HC
Gross Weight: 22,000 KG
Vessel: EVER GIVEN V.012
B/L No.: BL-773910
"""


@pytest.fixture
def client():
    return TestClient(app)


def test_stateless_analyze_ok(client):
    print("\n--- Test 1: Stateless Analyze (Matching SI vs BL) ---")
    payload = {
        "from": "forwarder@logistics.com",
        "subject": "TO CONFIRM DOCS _ BKG-883921 _ ROTTERDAM",
        "body": "Dear team, please verify the attached SI and draft BL for release.",
        "attachments": [
            {"filename": "Order_883921_SI.txt", "content_text": SAMPLE_SI_TEXT},
            {"filename": "Draft_BL_773910.txt", "content_text": SAMPLE_BL_MATCH_TEXT},
        ]
    }
    res = client.post("/api/v1/analyze", json=payload)
    assert res.status_code == 200, res.text
    data = res.json()
    print(f"Status: {data['status']}, Category: {data['category']}, "
          f"Has Defect: {data['has_defect']}")
    print(f"Action: {data['suggested_action']}")
    assert data["category"] == "BL_COMPARISON"
    assert data["status"] == "OK"
    assert data["has_defect"] is False
    assert len(data["defect_fields"]) == 0
    print("PASS: Stateless analyze correctly returned OK.")


def test_stateless_analyze_mismatch(client):
    print("\n--- Test 2: Stateless Analyze (Mismatch on container_count) ---")
    payload = {
        "from": "forwarder@logistics.com",
        "subject": "TO CONFIRM DOCS _ BKG-883921 _ ROTTERDAM",
        "body": "Please check the draft BL against SI.",
        "attachments": [
            {"filename": "Order_883921_SI.txt", "content_text": SAMPLE_SI_TEXT},
            {"filename": "Draft_BL_773910.txt", "content_text": SAMPLE_BL_MISMATCH_TEXT},
        ]
    }
    res = client.post("/api/v1/analyze", json=payload)
    assert res.status_code == 200, res.text
    data = res.json()
    print(f"Status: {data['status']}, Category: {data['category']}, "
          f"Defects: {data['defect_fields']}")
    print(f"Action: {data['suggested_action']}")
    assert data["status"] == "MISMATCH"
    assert data["has_defect"] is True
    assert "container_count" in data["defect_fields"]
    print("PASS: Stateless analyze correctly identified container_count mismatch.")


def test_stateful_ingest_and_review_desk(client):
    print("\n--- Test 3: Stateful Ingest & Review Desk Sync ---")
    email_id = "EXT-TEST-0099"
    payload = {
        "email_id": email_id,
        "from": "agent@ocean-carrier.com",
        "subject": "TO CONFIRM DOCS _ PO_9981 _ DRAFT BL",
        "body": "Dear team, please check SI against BL for booking PO_9981.",
        "attachments": [
            {"filename": "PO9981_SI.txt", "content_text": SAMPLE_SI_TEXT},
            {"filename": "PO9981_BL.txt", "content_text": SAMPLE_BL_MISMATCH_TEXT},
        ]
    }
    res = client.post("/api/v1/ingest", json=payload)
    assert res.status_code == 200, res.text
    data = res.json()
    print(f"Ingested ID: {data['email_id']}, Status: {data['status']}, "
          f"Desk URL: {data['review_desk_url']}")
    assert data["email_id"] == email_id
    assert data["persisted"] is True

    # The ingested email must be queryable through the Review Desk endpoint.
    detail_res = client.get(f"/api/emails/{email_id}")
    assert detail_res.status_code == 200, detail_res.text
    detail = detail_res.json()
    print(f"Found in Review Desk detail API: Category={detail['result']['category']}, "
          f"Status={detail['result']['status']}")
    assert detail["email"]["email_id"] == email_id
    assert detail["result"]["status"] == "MISMATCH"
    print("PASS: Ingested email persisted and synced to Review Desk.")


def test_security_gate_interception(client):
    print("\n--- Test 4: Security Gate Interception (PE / exe file spoofed) ---")
    fake_exe_content = b"MZ\x90\x00\x03\x00\x00\x00" + b"\x00" * 100
    b64_exe = base64.b64encode(fake_exe_content).decode("ascii")

    payload = {
        "from": "suspicious@unknown-sender.biz",
        "subject": "URGENT: BL Draft for Shipment",
        "body": "Please open the attached draft.",
        "attachments": [
            {"filename": "Draft_BL.pdf.exe", "content_base64": b64_exe}
        ]
    }
    res = client.post("/api/v1/analyze", json=payload)
    assert res.status_code == 200, res.text
    data = res.json()
    print(f"Security Category: {data['category']}, Status: {data['status']}")
    print(f"Security Alerts: {data['security_alerts']}")
    print(f"Action: {data['suggested_action']}")
    assert data["category"] == "SPAM"
    assert data["status"] == "ERROR"
    assert len(data["security_alerts"]) > 0
    print("PASS: Security gate successfully blocked malicious attachment.")


def test_non_bl_classification(client):
    print("\n--- Test 5: Ingesting Non-BL Email (Invoice Query) ---")
    payload = {
        "from": "billing@customer.com",
        "subject": "Missing GR for Invoice INV-88231 - Local Charges Query",
        "body": "Dear team, please advise on the local THC charges and reverse the PGI.",
        "attachments": []
    }
    res = client.post("/api/v1/analyze", json=payload)
    assert res.status_code == 200, res.text
    data = res.json()
    print(f"Category: {data['category']}, Status: {data['status']}, "
          f"Action: {data['suggested_action']}")
    assert data["category"] == "INVOICE_QUERY"
    assert data["status"] == "SKIPPED"
    print("PASS: Invoice query correctly routed without triggering BL comparison.")


def test_ingest_cannot_escape_the_ingest_directory(client, tmp_path):
    """A caller-supplied id or filename must stay inside INGEST_DIR.

    The endpoint is externally reachable and both values become path segments,
    so a traversal has to be neutralised rather than trusted.
    """
    print("\n--- Test 6: Ingest path traversal is neutralised ---")
    res = client.post("/api/v1/ingest", json={
        "email_id": "../../escape",
        "from": "attacker@example.com",
        "subject": "traversal attempt",
        "body": "compare the SI and BL",
        "attachments": [
            {"filename": "../../evil_SI.txt", "content_text": SAMPLE_SI_TEXT},
        ],
    })
    assert res.status_code == 200, res.text

    from app.config import get_settings

    ingest_root = Path(get_settings().ingest_dir)
    written = sorted(p.name for p in ingest_root.iterdir())
    print(f"Wrote under INGEST_DIR: {written}")
    assert written == ["escape"], written
    assert (ingest_root / "escape" / "evil_SI.txt").is_file()

    # Nothing may appear beside the temporary ingest directory.
    assert not (tmp_path / "escape").exists()
    assert not (tmp_path.parent / "escape").exists()
    print("PASS: traversal was contained inside INGEST_DIR.")


def test_api_health_contract(client):
    res = client.get("/api/health")
    assert res.status_code == 200, res.text
    body = res.json()
    # Base contract the integration docs promise…
    assert body["status"] == "ok"
    assert body["service"] == "ShipSync API"
    # …plus the result-cache bookkeeping (process-once-store-reuse).
    assert body["core"] == "existing-shipsync-core"
    assert isinstance(body["results_cached"], int)


def test_process_returns_structured_missing_bl_action(client):
    res = client.post("/api/process", json={
        "email_id": "GMAIL-001",
        "shipment_id": "SHP-001",
        "from": "customer@example.com",
        "subject": "SHP-001 please compare SI and BL",
        "body": "Please compare the SI and BL for SHP-001. BL appears to have been dropped.",
        "attachments": [
            {"filename": "Shipping_Instruction_SHP-001.txt", "content_text": SAMPLE_SI_TEXT},
        ],
    })
    assert res.status_code == 200, res.text
    data = res.json()
    assert data["shipment_id"] == "SHP-001"
    assert data["status"] == "NEEDS_REVIEW"
    assert data["missing_documents"] == ["BL"]
    assert data["action"]["type"] == "request_document"
    assert data["actions"][0]["document"] == "BL"
    assert any(doc["status"] == "missing" and doc["type"] == "BL" for doc in data["documents"])


def test_mock_customer_db_document_can_be_reprocessed(client):
    doc_res = client.post("/api/mock-customer-db/request-document", json={
        "shipment_id": "SHP-001",
        "document_type": "BL",
    })
    assert doc_res.status_code == 200, doc_res.text
    doc = doc_res.json()
    assert doc["found"] is True
    assert doc["attachment"]["filename"].endswith(".txt")

    process_res = client.post("/api/process", json={
        "email_id": "GMAIL-001-RERUN",
        "shipment_id": "SHP-001",
        "from": "customer@example.com",
        "subject": "SHP-001 please compare SI and BL",
        "body": "Please compare the SI and BL for SHP-001.",
        "attachments": [
            {"filename": "Shipping_Instruction_SHP-001.txt", "content_text": SAMPLE_SI_TEXT},
            doc["attachment"],
        ],
    })
    assert process_res.status_code == 200, process_res.text
    data = process_res.json()
    assert data["status"] == "MISMATCH"
    assert "container_count" in data["defect_fields"]
    assert data["verification"]["field_results"]


# --------------------------------------------------------------------------
# Process-once / store-reuse guarantees (Render traffic reduction).
# --------------------------------------------------------------------------
def _process_payload(**overrides):
    payload = {
        "email_id": "CACHE-001",
        "message_id": "MSG-CACHE-001",
        "thread_id": "THREAD-CACHE",
        "shipment_id": "SHP-CACHE",
        "from": "customer@example.com",
        "subject": "CACHE please compare SI and BL",
        "body": "Please compare the SI and BL. BL appears to have been dropped.",
        "attachments": [
            {"filename": "Order_883921_SI.txt", "content_text": SAMPLE_SI_TEXT},
        ],
        "source": "test",
    }
    payload.update(overrides)
    return payload


def test_same_email_is_processed_exactly_once(client):
    first = client.post("/api/process", json=_process_payload())
    assert first.status_code == 200, first.text
    assert first.json()["cached"] is False

    second = client.post("/api/process", json=_process_payload())
    assert second.status_code == 200, second.text
    body = second.json()
    # The stored result is returned untouched — no duplicate processing,
    # no duplicate shipment, no second OCR/extraction/verification run.
    assert body["cached"] is True
    assert body["status"] == first.json()["status"]
    assert body["attempts"] == 1


def test_force_is_the_only_way_to_reprocess(client):
    client.post("/api/process", json=_process_payload())
    forced = client.post("/api/process", json=_process_payload(force=True))
    assert forced.status_code == 200, forced.text
    assert forced.json()["cached"] is False
    assert forced.json()["attempts"] == 2


def test_changed_content_legitimately_reprocesses(client):
    client.post("/api/process", json=_process_payload())
    changed = _process_payload(attachments=[
        {"filename": "Order_883921_SI.txt", "content_text": SAMPLE_SI_TEXT},
        {"filename": "Draft_BL_773910.txt", "content_text": SAMPLE_BL_MISMATCH_TEXT},
    ])
    res = client.post("/api/process", json=changed)
    assert res.status_code == 200, res.text
    assert res.json()["cached"] is False
    assert res.json()["status"] == "MISMATCH"


def test_reading_a_result_never_processes(client):
    missing = client.get("/api/results/CACHE-404")
    assert missing.status_code == 404

    client.post("/api/process", json=_process_payload())
    for key in ("CACHE-001", "MSG-CACHE-001"):
        res = client.get(f"/api/results/{key}")
        assert res.status_code == 200, res.text
        assert res.json()["cached"] is True

    listed = client.get("/api/results")
    assert listed.status_code == 200, listed.text
    assert any(r["email_id"] == "CACHE-001" for r in listed.json())
