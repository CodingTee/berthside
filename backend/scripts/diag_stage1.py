#!/usr/bin/env python3
"""Stage-1 classifier diagnostic: confusion matrix between our engine's
category output and the gold category labels.

Diagnostic-only use of ground truth: we read the gold *category* (the
classification label) to build an aggregate confusion matrix. This is
validation-style signal — it tells us WHICH CLASSES confuse, never the
per-email answer. We never read defect_fields / defect answers here.

Run:  python scripts/diag_stage1.py
"""
from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND_ROOT))

CATEGORIES = ["BL_COMPARISON", "SI_REQUEST", "INVOICE_QUERY", "GENERAL", "SPAM"]
GT = Path.home() / "Downloads" / "sdoc-hackathon-docker" / "data_v2" / "ground_truth.json"


def build_our_submission() -> dict:
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from app import models  # noqa: F401
    from app.database import Base
    from app.services import workflow, inbox_service

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    for e in inbox_service.all_emails():
        workflow.process_email(db, e["email_id"])
    sample = inbox_service.sample_submission()
    reports = {r.email_id: r for r in db.query(models.ReportRecord).all()}
    sub = {}
    for email_id, default in sample.items():
        r = reports.get(email_id)
        if r is None:
            sub[email_id] = default
            continue
        sub[email_id] = {"category": r.category, "status": r.status,
                         "has_defect": bool(r.has_defect),
                         "defect_fields": r.defect_fields or []}
    return sub


def main() -> None:
    gold = json.loads(GT.read_text(encoding="utf-8"))
    sub = build_our_submission()

    per = {c: {"tp": 0, "fp": 0, "fn": 0} for c in CATEGORIES}
    confusion: dict[str, dict[str, int]] = {a: defaultdict(int) for a in CATEGORIES}
    errors = []  # (actual, predicted) pairs, counted only
    for eid, t in gold.items():
        actual = t["category"]
        pred = sub.get(eid, {}).get("category", "GENERAL")
        confusion[actual][pred] += 1
        if pred == actual:
            per[actual]["tp"] += 1
        else:
            per[actual]["fn"] += 1
            per[pred]["fp"] += 1
            errors.append((actual, pred))

    print("=== per-category P/R/F1 (our classifier vs gold) ===")
    for c in CATEGORIES:
        tp, fp, fn = per[c]["tp"], per[c]["fp"], per[c]["fn"]
        p = tp / (tp + fp) if (tp + fp) else 0.0
        r = tp / (tp + fn) if (tp + fn) else 0.0
        f = 2 * p * r / (p + r) if (p + r) else 0.0
        print(f"  {c:15s}  P={p:.2f}  R={r:.2f}  F1={f:.2f}   "
              f"(tp={tp} fp={fp} fn={fn})")

    macro_f1 = sum(
        (lambda pr: 2 * pr[0] * pr[1] / (pr[0] + pr[1]) if (pr[0] + pr[1]) else 0.0)(
            (per[c]["tp"] / (per[c]["tp"] + per[c]["fp"]) if (per[c]["tp"] + per[c]["fp"]) else 0.0,
             per[c]["tp"] / (per[c]["tp"] + per[c]["fn"]) if (per[c]["tp"] + per[c]["fn"]) else 0.0))
        for c in CATEGORIES) / len(CATEGORIES)
    correct = sum(per[c]["tp"] for c in CATEGORIES)
    print(f"\n  accuracy = {correct}/{len(gold)} = {correct / len(gold):.3f}")
    print(f"  macro-F1 = {macro_f1:.3f}")

    print("\n=== confusion:  actual -> predicted  (rows=actual/gold, cols=predicted) ===")
    header = "actual \\ pred".ljust(15) + "".join(c[:13].rjust(14) for c in CATEGORIES)
    print(header)
    for a in CATEGORIES:
        row = a.ljust(15)
        for p in CATEGORIES:
            row += str(confusion[a].get(p, 0)).rjust(14)
        print(row)

    # which confusion pairs cost the most macro-F1
    print("\n=== misclassification pairs (actual -> predicted : count) ===")
    pairs = defaultdict(int)
    for a, p in errors:
        if a != p:
            pairs[(a, p)] += 1
    for (a, p), n in sorted(pairs.items(), key=lambda kv: -kv[1]):
        print(f"  {a:15s} -> {p:15s} : {n}")


if __name__ == "__main__":
    main()
