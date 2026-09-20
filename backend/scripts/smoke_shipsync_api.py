"""Smoke test for the ShipSync integration API.

Proves the "process once, store, reuse" behaviour end to end:

    POST /api/process  -> runs the core, stores the result
    POST /api/process  -> same email, returns the stored result (cached=true)
    GET  /api/results/{id} -> reads the stored result, never processes
    POST /api/process with force=true -> explicit re-processing only

Usage:  python scripts/smoke_shipsync_api.py [base_url]
"""
from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000"


def call(path: str, payload: dict | None = None):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        BASE + path,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST" if payload is not None else "GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def main() -> int:
    print("health:", call("/api/health")[1])

    email = {
        "email_id": "SMOKE-001",
        "message_id": "MSG-SMOKE-001",
        "thread_id": "THREAD-SMOKE",
        "shipment_id": "SHP-001",
        "from": "customer@example.com",
        "subject": "SHP-001 compare SI and BL",
        "body": "Please compare the SI and BL for SHP-001.",
        "attachments": [
            {"filename": "SI_SHP-001.txt", "content_text": (
                "SHIPPING INSTRUCTION\n"
                "Shipper: EAST BRIGHT ENTERPRISE CO LTD\n"
                "Consignee: OCEANIC LOGISTICS GMBH\n"
                "Notify Party: PACIFIC FREIGHT SERVICES\n"
                "Port of Loading: NANTONG, CHINA (CNNTG)\n"
                "Port of Discharge: ROTTERDAM, NETHERLANDS (NLRTM)\n"
                "Total Containers: 3 x 40'HC\n"
                "Gross Weight: 22,000 KG\n")},
        ],
        "source": "smoke-test",
    }

    t0 = time.perf_counter()
    code, first = call("/api/process", email)
    print(f"\n1) POST /api/process           -> {code}")
    print("   status:", first.get("status"), "cached:", first.get("cached"),
          "ms:", round(first.get("processing_ms", 0), 1))
    print("   missing:", first.get("missing_documents"))
    print("   documents:", [(d["type"], d["version"]) for d in first.get("documents", [])])

    code, second = call("/api/process", email)
    print(f"\n2) POST /api/process (again)   -> {code}")
    print("   cached:", second.get("cached"), "attempts:", second.get("attempts"),
          "ms:", second.get("processing_ms"))
    assert second.get("cached") is True, "second call must be served from storage"

    code, stored = call("/api/results/SMOKE-001")
    print(f"\n3) GET  /api/results/SMOKE-001 -> {code}")
    print("   cached:", stored.get("cached"), "status:", stored.get("status"))

    email2 = dict(email, force=True)
    code, forced = call("/api/process", email2)
    print(f"\n4) POST /api/process force     -> {code}")
    print("   cached:", forced.get("cached"), "attempts:", forced.get("attempts"))
    assert forced.get("cached") is False, "force must re-run the core"

    # A changed attachment changes the content hash -> reprocessing is legit.
    changed = json.loads(json.dumps(email))
    changed["attachments"].append(
        {"filename": "Draft_BL_SHP-001.txt", "content_text": (
            "BILL OF LADING (DRAFT)\n"
            "Shipper: EAST BRIGHT ENTERPRISE CO LTD\n"
            "Consignee: OCEANIC LOGISTICS GMBH\n"
            "Notify Party: PACIFIC FREIGHT SERVICES\n"
            "Port of Loading: NANTONG, CHINA (CNNTG)\n"
            "Port of Discharge: ROTTERDAM, NETHERLANDS (NLRTM)\n"
            "Total Containers: 4 x 40'HC\n"
            "Gross Weight: 22,000 KG\n")}
    )
    code, updated = call("/api/process", changed)
    print(f"\n5) POST /api/process (new doc) -> {code}")
    print("   cached:", updated.get("cached"), "status:", updated.get("status"))
    print("   defects:", updated.get("defect_fields"))
    print("   versions:", [(d["type"], d["version"]) for d in updated.get("documents", [])])
    assert updated.get("cached") is False, "changed content must be reprocessed"

    code, listing = call("/api/results")
    print(f"\n6) GET  /api/results           -> {code} ({len(listing)} stored)")

    print(f"\nOK in {time.perf_counter() - t0:.1f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
