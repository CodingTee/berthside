"""Verify the shipment-centred demo dataset end to end.

Runs the ten checks the iteration was specified against, against a live backend:

    1  complete shipment            SHP-001 -> VERIFIED
    2  missing BL                   SHP-002 -> NEEDS_ATTENTION / Missing BL
    3  multiple SI versions         SHP-003 -> v1,v2,v3 kept, v3 current
    4  mismatch                     SHP-004 -> container_count disagreement
    5  multiple emails              SHP-005 -> one shipment, several senders
    6  file formats                 every format the reader claims is really read
    7  multi-page document          SHP-001 BL: fields live on page 2
    8  processing efficiency        reading shipments re-runs nothing
    9  shipment data completeness   SHP-003 payload has everything a UI needs
    10 missing-BL payload           SHP-002 documents/state/action shape

Usage
-----
    python scripts/verify_demo_shipments.py
    python scripts/verify_demo_shipments.py --base http://127.0.0.1:8000
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parent.parent

PASS, FAIL = [], []


def check(name: str, ok: bool, detail: str = "") -> bool:
    (PASS if ok else FAIL).append(name)
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))
    return ok


def get_json(url: str) -> dict:
    import urllib.request

    with urllib.request.urlopen(url, timeout=120) as resp:
        return json.loads(resp.read().decode())


def overview(base: str, code: str) -> dict:
    return get_json(f"{base}/shipments/by-key/{code}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--base", default="http://127.0.0.1:8000")
    ap.add_argument("--data", default=str(BACKEND_ROOT / "demo-shipments"))
    args = ap.parse_args()
    base = args.base.rstrip("/")

    # ---------------------------------------------------------------- TEST 1
    print("TEST 1  complete shipment (SHP-001)")
    s1 = overview(base, "SHP-001")
    check("SHP-001 verified", s1["status"] == "VERIFIED", f"status={s1['status']}")
    check("SHP-001 document set complete",
          all(s1["documents"][t]["present"] for t in ("SI", "BL", "INVOICE")),
          f"types={s1['document_types']}")
    check("SHP-001 no mismatch", not s1["mismatch_fields"] and s1["issue_count"] == 0)

    # ---------------------------------------------------------------- TEST 2
    print("TEST 2  missing BL (SHP-002)")
    s2 = overview(base, "SHP-002")
    check("SHP-002 needs attention", s2["status"] == "NEEDS_ATTENTION",
          f"status={s2['status']}")
    check("missing BL reported at shipment level",
          s2["missing_documents"] == ["BL"] and not s2["complete"],
          f"missing={s2['missing_documents']}")
    check("issue is a shipment reason, not a document type",
          any("Bill of Lading" in r for r in s2["reasons"]), f"reasons={s2['reasons']}")
    check("no fake BL document created",
          s2["documents"]["BL"]["version_count"] == 0
          and s2["documents"]["BL"]["present"] is False)
    check("Request BL action available", "REQUEST_BL" in s2["actions"],
          f"actions={s2['actions']}")

    # ---------------------------------------------------------------- TEST 3
    print("TEST 3  multiple SI versions (SHP-003)")
    s3 = overview(base, "SHP-003")
    si = s3["documents"]["SI"]
    check("three SI versions preserved", si["version_count"] == 3,
          f"versions={si['version_count']}")
    check("v3 is current", si["current_version_number"] == 3,
          f"current={si['current_version_number']}")
    # Only ACTIVE rows are versions; a re-sent identical document is stored as a
    # DUPLICATE for audit and deliberately does not advance the version number,
    # so it must not be counted here.
    active = sorted((v for v in si["versions"] if v["document_status"] == "ACTIVE"),
                    key=lambda v: v["version_number"])
    weights = [v["extracted_fields"].get("gross_weight_kg") for v in active]
    check("old versions not overwritten", weights == [38900.0, 40150.0, 41500.0],
          f"weights={weights}")
    check("BL compared against the current version",
          s3["verification"]["status"] == "OK" and not s3["mismatch_fields"],
          f"verification={s3['verification']['status']}")

    # ---------------------------------------------------------------- TEST 4
    print("TEST 4  mismatch (SHP-004)")
    s4 = overview(base, "SHP-004")
    check("SHP-004 needs attention", s4["status"] == "NEEDS_ATTENTION",
          f"status={s4['status']}")
    check("container mismatch identified",
          "container_count" in s4["mismatch_fields"],
          f"mismatch={s4['mismatch_fields']}")
    issue = s4["issues"][0] if s4["issues"] else {}
    check("mismatch is specific (si vs bl values)",
          issue.get("si_value") == 3 and issue.get("bl_value") == 4,
          f"si={issue.get('si_value')} bl={issue.get('bl_value')}")

    # ---------------------------------------------------------------- TEST 5
    print("TEST 5  multiple emails grouped under one shipment (SHP-005)")
    s5 = overview(base, "SHP-005")
    senders = {e["sender"] for e in s5["source_emails"]}
    check("several source emails", len(s5["source_emails"]) >= 2,
          f"emails={len(s5['source_emails'])}")
    check("from different senders", len(senders) >= 2, f"senders={sorted(senders)}")
    check("ZIP members became real documents",
          s5["documents"]["SI"]["present"] and s5["documents"]["BL"]["present"],
          f"types={s5['document_types']}")
    check("revised invoice became version 2",
          s5["documents"]["INVOICE"]["version_count"] == 2,
          f"invoice versions={s5['documents']['INVOICE']['version_count']}")

    # ---------------------------------------------------------------- TEST 6
    print("TEST 6  file-format coverage (real reads, no faking)")
    sys.path.insert(0, str(BACKEND_ROOT))
    from app.services import ai_service, archive  # noqa: E402

    att = Path(args.data) / "attachments"
    expected = {
        "SHP-003_SI_v1.txt": "txt",
        "SHP-002_SI.csv": "csv",
        "SHP-004_SI.xml": "xml",
        "SHP-002_Invoice.xlsx": "xlsx",
        "SHP-003_SI_v3.docx": "docx",
        "SHP-001_SI.pdf": "pdf",
        "SHP-005_Invoice_rev2.jpeg": "image-ocr",
        "SHP-006_BL.tiff": "image-ocr",
    }
    for name, want in expected.items():
        text, source = ai_service.document_text(name, (att / name).read_bytes())
        check(f"{name} read as {want}", bool(text) and source == want,
              f"source={source} chars={len(text or '')}")
    expansion = archive.expand_archive(
        "SHP-005_shipment_documents.zip",
        (att / "SHP-005_shipment_documents.zip").read_bytes())
    members = expansion.members
    check("ZIP expanded into documents", len(members) >= 3,
          f"members={[m for m, _ in members]} notes={expansion.notes}")
    check("no member was blocked by the safety gate", not expansion.blocked,
          f"blocked={expansion.blocked}")
    check("ZIP members are readable",
          all(ai_service.document_text(n, c)[0] for n, c in members))

    # ---------------------------------------------------------------- TEST 7
    print("TEST 7  multi-page document")
    text, _ = ai_service.document_text("SHP-001_BL.pdf",
                                       (att / "SHP-001_BL.pdf").read_bytes())
    from app.services import extractor  # noqa: E402

    fields = extractor.extract_fields(text, "BL").fields
    check("fields found past page 1",
          fields.get("shipper") == "NORDIC PAPER ASIA SDN BHD",
          f"shipper={fields.get('shipper')}")
    check("all compared fields read from the multi-page BL",
          all(fields.get(f) is not None for f in
              ("shipper", "consignee", "notify_party", "port_of_loading",
               "port_of_discharge", "container_count", "gross_weight_kg")))

    # ---------------------------------------------------------------- TEST 8
    print("TEST 8  reading shipments triggers no reprocessing")
    sys.path.insert(0, str(BACKEND_ROOT))
    from app.database import SessionLocal  # noqa: E402
    from app.models import ReportRecord  # noqa: E402

    db = SessionLocal()
    before = {r.email_id: (r.attempts, r.processing_ms)
              for r in db.query(ReportRecord).filter(
                  ReportRecord.email_id.like("demo-%")).all()}
    db.close()
    t0 = time.perf_counter()
    for _ in range(3):
        get_json(f"{base}/shipments/overview")
        overview(base, "SHP-003")
        overview(base, "SHP-002")
    elapsed = (time.perf_counter() - t0) * 1000.0
    db = SessionLocal()
    after = {r.email_id: (r.attempts, r.processing_ms)
             for r in db.query(ReportRecord).filter(
                 ReportRecord.email_id.like("demo-%")).all()}
    db.close()
    check("no email was reprocessed", before == after,
          f"9 shipment reads in {elapsed:.0f}ms")
    check("shipment reads are cheap (no OCR/extraction)", elapsed < 5000,
          f"{elapsed:.0f}ms for 9 reads")

    # ---------------------------------------------------------------- TEST 9
    print("TEST 9  shipment data completeness (SHP-003)")
    needed = ["shipment_id", "shipment_code", "status", "documents",
              "verification", "source_emails", "actions", "missing_documents"]
    check("payload carries every field a shipment view needs",
          all(k in s3 for k in needed), f"keys={sorted(s3)[:8]}...")
    check("SI history present", len(s3["documents"]["SI"]["versions"]) >= 3)
    check("BL and invoice present",
          s3["documents"]["BL"]["present"] and s3["documents"]["INVOICE"]["present"])
    check("source email metadata preserved",
          all(e.get("sender") and e.get("subject") and e.get("attachments")
              for e in s3["source_emails"]),
          f"emails={len(s3['source_emails'])}")

    # --------------------------------------------------------------- TEST 10
    print("TEST 10 missing-BL payload shape (SHP-002)")
    check("SI available", s2["documents"]["SI"]["present"])
    check("BL absent, not fabricated",
          s2["documents"]["BL"]["present"] is False)
    check("invoice available", s2["documents"]["INVOICE"]["present"])
    check("status is Needs Attention", s2["status"] == "NEEDS_ATTENTION")
    check("action is Request BL", "REQUEST_BL" in s2["actions"])

    print("-" * 60)
    print(f"{len(PASS)} passed, {len(FAIL)} failed")
    for name in FAIL:
        print(f"  FAILED: {name}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
