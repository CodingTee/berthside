#!/usr/bin/env python3
"""Diagnostic: which gold-defect emails do we miss at end-to-end, and why?

We rebuild our prediction the same way the API does, score it with the
official scorer's logic, and for each missed defect email we inspect ONLY our
own engine output (category / status / has_defect / defect_fields /
field_results) to find the failure mode. Gold field values are consulted
solely to label *which* of the three E2E conditions failed (routing /
flagging / exact-field-set). No rule is hardcoded from this.
"""
from __future__ import annotations
import json, sys
from pathlib import Path
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

BACKEND_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BACKEND_ROOT))
sys.path.insert(0, str(SCRIPTS_DIR))

import sdoc_paths  # noqa: E402  (needs the sys.path setup above)

from app import models  # noqa: F401
from app.database import Base
from app.services import workflow, inbox_service

_gt = sdoc_paths.default_ground_truth()
if not _gt:
    raise SystemExit(sdoc_paths.missing_file_hint("ground_truth.json"))
GT = Path(_gt)

engine = create_engine("sqlite:///:memory:")
Base.metadata.create_all(engine)
db = sessionmaker(bind=engine)()

emails = inbox_service.all_emails()
for e in emails:
    workflow.process_email(db, e["email_id"])

reports = {r.email_id: r for r in db.query(models.ReportRecord).all()}
truth = json.loads(GT.read_text())

# Build our submission (same shape as tune_eval)
sample = inbox_service.sample_submission()
sub = {}
for email_id, default in sample.items():
    r = reports.get(email_id)
    if r is None:
        sub[email_id] = default
    else:
        sub[email_id] = {
            "category": r.category,
            "status": r.status if r.status != "ERROR" else "NEEDS_REVIEW",
            "review_reason": r.review_reason,
            "has_defect": bool(r.has_defect),
            "defect_fields": r.defect_fields or [],
        }

# Walk E2E exactly like scoring.score_end_to_end
print("=== GOLD DEFECT EMAILS MISSED AT E2E ===")
missed = []
for eid, t in truth.items():
    if not (t["category"] == "BL_COMPARISON" and t.get("has_defect")):
        continue
    s = sub.get(eid, {})
    routed = s.get("category") == "BL_COMPARISON"
    flagged = bool(s.get("has_defect"))
    fields_ok = set(s.get("defect_fields", [])) == set(t["defect_fields"])
    if not (routed and flagged and fields_ok):
        missed.append((eid, routed, flagged, fields_ok))

print(f"missed {len(missed)} / 46\n")
for eid, routed, flagged, fields_ok in missed:
    r = reports.get(eid)
    print(f"--- {eid} ---")
    print(f"  routed_BL={routed}  flagged={flagged}  fields_ok={fields_ok}")
    print(f"  OUR: category={r.category} status={r.status} reason={r.review_reason}")
    print(f"  OUR has_defect={bool(r.has_defect)} defect_fields={r.defect_fields}")
    print(f"  GOLD defect_fields={truth[eid]['defect_fields']}")
    print(f"  OUR field_results:")
    for fr in (r.field_results or []):
        print(f"     {fr.get('field'):<18} match={fr.get('match')} "
              f"si={str(fr.get('si_value'))[:28]!r} bl={str(fr.get('bl_value'))[:28]!r} "
              f"missing={fr.get('missing')}")
    print()
