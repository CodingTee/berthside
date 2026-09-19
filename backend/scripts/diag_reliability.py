#!/usr/bin/env python3
"""Diagnose the reliability (human-review) axis with the OFFICIAL scorer.

    python scripts/diag_reliability.py
    python scripts/diag_reliability.py --data-source ../sdoc-hackathon-bundle

Prints the official escalation precision/recall (scoring.score_reliability),
then breaks every FALSE-POSITIVE escalation down by our review_reason, so we
can see which gate is over-triggering and on what kind of email.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND_ROOT))

MATERIALS = Path(r"C:\Users\gayso\Desktop\Hackathon Averix\materials\sdoc-hackathon-docker")
DEFAULT_GT = MATERIALS / "data_v2" / "ground_truth.json"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-source", default="")
    ap.add_argument("--ground-truth", default=str(DEFAULT_GT))
    ap.add_argument("--submission", default="", help="reuse an existing submission.json")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--show", type=int, default=40, help="how many examples to print")
    args = ap.parse_args()

    if args.data_source:
        import os
        os.environ["DATA_SOURCE"] = args.data_source

    # Load the organisers' scorer by file path — importing it via sys.path would
    # shadow our own `app` package with the docker server's `app.py`.
    import importlib.util
    _spec = importlib.util.spec_from_file_location("sdoc_scoring", MATERIALS / "server" / "scoring.py")
    scoring = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(scoring)

    truth = json.loads(Path(args.ground_truth).read_text(encoding="utf-8"))

    if args.submission:
        sub = json.loads(Path(args.submission).read_text(encoding="utf-8"))
    else:
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker
        from app import models
        from app.database import Base
        from app.services import inbox_service, workflow

        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(engine)
        db = sessionmaker(bind=engine)()
        emails = inbox_service.all_emails()
        if args.limit:
            emails = emails[:args.limit]
        for e in emails:
            workflow.process_email(db, e["email_id"])
        reports = {r.email_id: r for r in db.query(models.ReportRecord).all()}
        sample = inbox_service.sample_submission()
        sub = {}
        for eid, default in sample.items():
            r = reports.get(eid)
            if r is None:
                sub[eid] = default
                continue
            sub[eid] = {
                "category": r.category,
                "status": r.status if r.status != "ERROR" else "NEEDS_REVIEW",
                "review_reason": r.review_reason,
                "has_defect": bool(r.has_defect),
                "defect_fields": r.defect_fields or [],
            }

    rel = scoring.score_reliability(truth, sub)
    print("=" * 70)
    print("RELIABILITY (official scorer)")
    print("=" * 70)
    print(f"  escalation recall     {rel['escalation_recall']:.3f}")
    print(f"  escalation precision  {rel['escalation_precision']:.3f}")
    print(f"  escalation f1         {rel['escalation_f1']:.3f}")
    print(f"  gold NEEDS_REVIEW {rel['gold_review']}  vs  flagged {rel['pred_review']}")
    print(f"  correct flags: {round(rel['escalation_precision'] * rel['pred_review'])}")
    print("\n  per gold reason (caught/total):")
    for rn, d in rel["per_reason"].items():
        print(f"    {rn:<20} {d['caught']}/{d['total']}")

    fp = [eid for eid, s in sub.items()
          if s.get("status") == "NEEDS_REVIEW"
          and truth.get(eid, {}).get("status") != "NEEDS_REVIEW"]
    print(f"\n  false-positive escalations: {len(fp)}")

    by_reason = defaultdict(list)
    for eid in fp:
        by_reason[sub[eid].get("review_reason") or "(none)"].append(eid)

    print("\n  breakdown by OUR review_reason:")
    for reason, ids in sorted(by_reason.items(), key=lambda kv: -len(kv[1])):
        gold_status = Counter(truth.get(i, {}).get("status") for i in ids)
        gold_cat = Counter(truth.get(i, {}).get("category") for i in ids)
        print(f"\n  --- {reason}: {len(ids)}")
        print(f"      gold status: {dict(gold_status)}")
        print(f"      gold category: {dict(gold_cat)}")

    print("\n  examples:")
    for reason, ids in sorted(by_reason.items(), key=lambda kv: -len(kv[1])):
        print(f"\n  == {reason} ==")
        for eid in ids[: args.show]:
            t = truth.get(eid, {})
            s = sub[eid]
            print(f"    {eid}  gold={t.get('status')}/{t.get('category')}  "
                  f"defect={t.get('has_defect')}  our_reason={s.get('review_reason')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
