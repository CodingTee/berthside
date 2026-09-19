#!/usr/bin/env python3
"""Offline evaluation loop: run the pipeline + score it, no server needed.

    python scripts/tune_eval.py                     # full inbox, score
    python scripts/tune_eval.py --limit 60          # quick smoke run
    python scripts/tune_eval.py --out sub.json      # keep the submission

Why this exists: the self-evaluation endpoint is only reachable through the
official Docker server, but the organisers ship `score_cli.py` + the private
ground_truth.json in the docker bundle, so we can score locally in seconds and
iterate on the classifier without rebuilding anything.

The pipeline, the comparison engine and the submission builder are the *same*
code the API runs — only the transport is skipped.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND_ROOT))

DEFAULT_SCORER = Path.home() / "Downloads" / "sdoc-hackathon-docker" / "server" / "score_cli.py"
DEFAULT_GT = Path.home() / "Downloads" / "sdoc-hackathon-docker" / "data_v2" / "ground_truth.json"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--out", default=str(BACKEND_ROOT / "submission.json"))
    ap.add_argument("--scorer", default=str(DEFAULT_SCORER))
    ap.add_argument("--ground-truth", default=str(DEFAULT_GT))
    ap.add_argument("--db", default="")
    args = ap.parse_args()

    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from app import models  # noqa: F401
    from app.database import Base
    from app.services import workflow
    from app.services import inbox_service

    if args.db:
        engine = create_engine(f"sqlite:///{args.db}")
    else:
        engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()

    emails = inbox_service.all_emails()
    if args.limit:
        emails = emails[:args.limit]

    t0 = time.perf_counter()
    by_status: dict[str, int] = {}
    for e in emails:
        r = workflow.process_email(db, e["email_id"])
        by_status[r.status] = by_status.get(r.status, 0) + 1
    elapsed = time.perf_counter() - t0

    # Build the submission in sample_submission shape.
    sample = inbox_service.sample_submission()
    reports = {r.email_id: r for r in db.query(models.ReportRecord).all()}
    submission = {}
    for email_id, default in sample.items():
        r = reports.get(email_id)
        if r is None:
            submission[email_id] = default
            continue
        submission[email_id] = {
            "category": r.category,
            "status": r.status if r.status != "ERROR" else "NEEDS_REVIEW",
            "review_reason": r.review_reason,
            "has_defect": bool(r.has_defect),
            "defect_fields": r.defect_fields or [],
        }

    Path(args.out).write_text(json.dumps(submission, indent=2), encoding="utf-8")
    print(f"processed {len(emails)} emails in {elapsed:.1f}s -> {args.out}")
    print("by_status:", json.dumps(by_status, sort_keys=True))

    scorer = Path(args.scorer)
    if not scorer.exists():
        print(f"\nscorer not found: {scorer}\n"
              f"  pass --scorer <path to score_cli.py>")
        return 1

    import subprocess
    cmd = [sys.executable, str(scorer), args.out,
           "--ground-truth", args.ground_truth]
    res = subprocess.run(cmd, capture_output=True, text=True)
    print()
    print(res.stdout or res.stderr)
    return res.returncode


if __name__ == "__main__":
    raise SystemExit(main())
