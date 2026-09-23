#!/usr/bin/env python3
"""End-to-end smoke test — proves the whole pipeline works.

    python scripts/smoke_test.py                 # assumes http://127.0.0.1:8000
    python scripts/smoke_test.py http://localhost:8000

Checks:
  1. /health                      API + DB + data source reachable
  2. GET /emails                  inbox loads (520 emails)
  3. POST /emails/{id}/process    classify → extract → compare on a known email
  4. comparison determinism       expected MISMATCH + expected defect fields
  5. GET /reports/{id}            report persisted with per-field evidence
  6. POST /reviews/{id}           human-in-the-loop decision is applied
  7. GET /reports/summary/stats   aggregates over the whole inbox
  8. GET /reports/submission/json every email present in submission shape

Exit code 0 = all checks passed.
"""
from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from typing import Any

BASE = (sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000").rstrip("/")

# Ground truth we verified by hand from
# attachments/email_004_SI.txt vs attachments/email_004_BL.txt:
#   consignee:     EAST BRIGHT FZ-LLC  vs  UAB NOVAKOPA
#   notify_party:  EAST BRIGHT FZ-LLC  vs  UAB NOVAKOPA
#   everything else identical
CASE_EMAIL_ID = "email_004"
EXPECTED_CATEGORY = "BL_COMPARISON"
EXPECTED_STATUS = "MISMATCH"
EXPECTED_DEFECTS = {"consignee", "notify_party"}

passed = failed = 0


def check(name: str, ok: bool, detail: str = "") -> bool:
    global passed, failed
    tag = "PASS" if ok else "FAIL"
    if ok:
        passed += 1
    else:
        failed += 1
    print(f"[{tag}] {name}" + (f"  — {detail}" if detail else ""))
    return ok


def get(path: str) -> Any:
    with urllib.request.urlopen(f"{BASE}{path}", timeout=60) as r:
        return json.loads(r.read())


def post(path: str, payload: dict | None = None, method: str = "POST") -> Any:
    data = json.dumps(payload).encode() if payload is not None else b""
    req = urllib.request.Request(
        f"{BASE}{path}", data=data, method=method,
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.loads(r.read())


def main() -> int:
    print(f"Smoke test against {BASE}\n{'-' * 60}")

    # 1 ---------------------------------------------------------------------
    health = get("/health")
    check("1. /health reachable", health.get("status") == "ok",
          f"db={health.get('database')} ai={health.get('ai_provider')} "
          f"reports={health.get('reports_stored')}")

    # 2 ---------------------------------------------------------------------
    listing = get("/emails?limit=5")
    total = listing.get("total", 0)
    check("2. GET /emails loads the inbox", total > 0, f"{total} emails")

    # 3 ---------------------------------------------------------------------
    result = post(f"/emails/{CASE_EMAIL_ID}/process")
    got_cat = result.get("category")
    got_status = result.get("status")
    got_defects = set(result.get("defect_fields") or [])
    check("3. process classifies correctly",
          got_cat == EXPECTED_CATEGORY, f"category={got_cat}")
    check("4. deterministic comparison gives the expected verdict",
          got_status == EXPECTED_STATUS and got_defects == EXPECTED_DEFECTS,
          f"status={got_status} defects={sorted(got_defects)}")

    # 5 ---------------------------------------------------------------------
    report = get(f"/reports/{CASE_EMAIL_ID}")
    fields = {f["field"]: f["match"] for f in (report.get("field_results") or [])}
    check("5. report persisted with per-field evidence",
          len(fields) == 7 and fields.get("shipper") is True
          and fields.get("container_count") is True,
          f"{len(fields)} fields compared")

    # 6 ---------------------------------------------------------------------
    review = post(f"/reviews/{CASE_EMAIL_ID}", {
        "decision": "CONFIRM", "reviewer": "smoke-test",
        "notes": "auto verification"})
    report2 = get(f"/reports/{CASE_EMAIL_ID}")
    check("6. human review applied", bool(report2.get("reviewed")),
          f"review id={review.get('id')} decision={review.get('decision')}")

    # 7 ---------------------------------------------------------------------
    stats = get("/reports/summary/stats")
    check("7. aggregates available", stats.get("total_reports", 0) > 0,
          f"by_status={stats.get('by_status')}")
    check("7b. no pipeline errors", stats.get("errors", 1) == 0,
          f"errors={stats.get('errors')}")

    # 8 ---------------------------------------------------------------------
    sub = get("/reports/submission/json")
    ok_shape = (sub.get("emails_covered") == sub.get("total_inbox")
                and all(set(v) >= {"category", "status", "has_defect",
                                   "defect_fields", "review_reason"}
                        for v in sub["submission"].values()))
    check("8. submission covers every email in the right shape", ok_shape,
          f"{sub.get('emails_covered')}/{sub.get('total_inbox')} emails")

    print("-" * 60)
    print(f"{passed} passed, {failed} failed")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except urllib.error.URLError as exc:
        print(f"\nCannot reach {BASE} — is the server running?\n"
              f"  cd backend && uvicorn app.main:app --reload --port 8000\n"
              f"({exc})")
        sys.exit(2)
