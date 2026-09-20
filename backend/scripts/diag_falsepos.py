#!/usr/bin/env python3
"""Find emails where our next-line port grab produced a false-positive defect
(we say MISMATCH but gold says no defect), and inspect the offending ports."""
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
truth = json.loads(GT.read_text())

engine = create_engine("sqlite:///:memory:")
Base.metadata.create_all(engine)
db = sessionmaker(bind=engine)()
for e in inbox_service.all_emails():
    workflow.process_email(db, e["email_id"])

reports = {r.email_id: r for r in db.query(models.ReportRecord).all()}

false_pos = []
for eid, t in truth.items():
    if t["category"] != "BL_COMPARISON":
        continue
    if t.get("status") == "NEEDS_REVIEW":
        continue
    r = reports.get(eid)
    if r is None:
        continue
    pred_defect = bool(r.has_defect) and r.category == "BL_COMPARISON"
    gold_defect = t["has_defect"]
    if pred_defect and not gold_defect:
        false_pos.append(eid)

print(f"false-positive MISMATCH vs gold-OK: {len(false_pos)}")
for eid in false_pos:
    r = reports[eid]
    print(f"--- {eid}: status={r.status} has_defect={bool(r.has_defect)} "
          f"defect_fields={r.defect_fields}")
    for fr in (r.field_results or []):
        if fr.get("field") in ("port_of_loading", "port_of_discharge"):
            print(f"     {fr['field']}: match={fr.get('match')} "
                  f"si={str(fr.get('si_value'))[:30]!r} bl={str(fr.get('bl_value'))[:30]!r}")
